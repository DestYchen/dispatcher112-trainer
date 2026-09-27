import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assignment,
    AuditLog,
    IncidentType,
    Lesson,
    PhoneReport,
    Scenario,
    Service,
    StatusEvent,
)
from app.domain.card_entry import compare_fields
from app.domain.criteria import assess_criteria
from app.domain.enums import MODIFIER_LABELS
from app.scoring.engine import evaluate
from app.scoring.grammar import check_grammar
from app.scoring.rules.address import check_addresses
from app.scoring.types import ScoreInput


async def score_assignment(
    db: AsyncSession,
    assignment: Assignment,
    lesson: Lesson,
    events: Sequence[StatusEvent] | None = None,
) -> dict[str, Any]:
    if assignment.score is not None:
        return assignment.score
    assert assignment.delivered_at is not None and assignment.closed_at is not None
    scenario, incident = (
        await db.execute(
            select(Scenario, IncidentType)
            .join(IncidentType)
            .where(Scenario.id == assignment.scenario_id)
        )
    ).one()
    if events is None:
        events = list(
            await db.scalars(
                select(StatusEvent)
                .where(StatusEvent.assignment_id == assignment.id)
                .order_by(StatusEvent.created_at, StatusEvent.id)
            )
        )
    reports = list(
        await db.scalars(
            select(PhoneReport)
            .where(PhoneReport.assignment_id == assignment.id)
            .order_by(PhoneReport.created_at, PhoneReport.id)
        )
    )
    services = (
        await db.execute(
            select(Service.name, Service.short_name, Service.code).order_by(Service.code)
        )
    ).all()
    texts = [
        (event.comment, "comment", event.created_at.isoformat())
        for event in events
        if event.comment
    ]
    texts += [
        (report.transcript, "report", report.created_at.isoformat())
        for report in reports
        if report.transcript
    ]
    entry_fields = None
    if assignment.task_mode == "CARD_ENTRY":
        answer = assignment.card_submission or assignment.card_draft or {}
        expected = {**scenario.card_payload, "incident_type_id": str(scenario.incident_type_id)}
        visible = set(await db.scalars(select(Service.code).where(Service.is_visible.is_(True))))
        expected["notified_services"] = [
            code for code in expected.get("notified_services", []) if code in visible
        ]
        entry_fields = tuple(
            compare_fields(
                answer, expected, scenario.reference.get("entry_description_keywords", [])
            )
        )
        texts += [
            # Entry text is checked alongside comments with the same local grammar/address rules.
            (
                str(answer.get("address", {}).get("raw", "")),
                "address",
                assignment.closed_at.isoformat(),
            ),
            (str(answer.get("description", "")), "description", assignment.closed_at.isoformat()),
        ]
        entered_type = (
            await db.get(IncidentType, UUID(answer["incident_type_id"]))
            if answer.get("incident_type_id")
            else None
        )
        service_names = {code: name for name, _, code in services}
        for item in entry_fields:
            if item["field"] == "incident_type_id":
                item.update(
                    actual_display=entered_type.name if entered_type else "Не выбран",
                    expected_display=incident.name,
                )
            if item["field"] in {"modifiers", "notified_services"}:
                labels = MODIFIER_LABELS if item["field"] == "modifiers" else service_names
                for side in ("actual", "expected"):
                    item[side + "_display"] = (
                        ", ".join(labels.get(code, code) for code in item[side] or []) or "Нет"
                    )
    grammar_items: list[dict[str, Any]] = []
    address_items: list[dict[str, Any]] = []
    available = lesson.settings["grammar_check_enabled"]
    reason = None
    if available and not texts:
        probe = await check_grammar("")
        available, reason = probe.available, probe.reason
    for text, field, at in texts:
        assert text is not None
        if available:
            addresses, grammar = await asyncio.gather(
                check_addresses(db, text, scenario.card_payload["address"]["raw"]),
                check_grammar(text, field),
            )
        else:
            addresses = await check_addresses(db, text, scenario.card_payload["address"]["raw"])
        address_items.extend({**item, "field": field, "at": at} for item in addresses)
        if available:
            available, reason = grammar.available, grammar.reason
            # A street spelling issue is counted once, with its stronger address classification.
            for item in grammar.items:
                if not any(
                    item["offset"] < address["offset"] + address["length"]
                    and address["offset"] < item["offset"] + item["length"]
                    for address in addresses
                ):
                    grammar_items.append({**item, "at": at})
    data = ScoreInput(
        delivered_at=assignment.delivered_at,
        opened_at=assignment.opened_at,
        primary_status_at=assignment.primary_status_at,
        closed_at=assignment.closed_at,
        settings=lesson.settings,
        reference=scenario.reference,
        events=tuple(
            {
                "status": event.status,
                "comment": event.comment,
                "is_automatic": event.is_automatic,
                "at": event.created_at.isoformat(),
            }
            for event in events
        ),
        reports=tuple(
            {"callee_code": report.callee_code, "transcript": report.transcript}
            for report in reports
        ),
        services=tuple(
            name
            for service in services
            for name in (service.name, service.short_name, service.code)
        ),
        card_number=assignment.card_number,
        address=scenario.card_payload["address"]["raw"],
        incident_type_name=incident.name,
        grammar_items=tuple(grammar_items),
        grammar_available=available,
        grammar_skip_reason=reason,
        address_items=tuple(address_items),
        entry_fields=entry_fields,
        card_submitted=assignment.submitted_at is not None,
    )
    result = evaluate(data)
    if lesson.settings.get("success_criteria"):
        result["criteria"] = assess_criteria(
            result,
            [value for value, field, _ in texts if field != "address"],
            lesson.settings["success_criteria"],
        )
    assignment.score = result
    db.add(
        AuditLog(
            action="SCORE_COMPUTED",
            entity_type="assignment",
            entity_id=assignment.id,
            payload={"total": result["total"], "engine_version": result["engine_version"]},
        )
    )
    return result
