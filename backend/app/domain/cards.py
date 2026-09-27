from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import APIError
from app.db.models import Assignment, IncidentType, Lesson, Scenario, Service, StatusEvent, User
from app.domain.card_entry import EntryCardInput, incoming_message
from app.domain.enums import MODIFIER_LABELS, STATUS_LABELS, ResponseStatus
from app.domain.status_machine import available_transitions


def lesson_payload(lesson: Lesson) -> dict[str, Any]:
    return {
        "id": str(lesson.id),
        "title": lesson.title,
        "status": lesson.status,
        "settings": lesson.settings,
    }


def timers(assignment: Assignment, lesson: Lesson, now: datetime) -> dict[str, Any]:
    return {
        "server_time": now.isoformat(),
        "primary_deadline_at": (
            assignment.delivered_at
            + timedelta(seconds=lesson.settings["primary_status_deadline_sec"])
        ).isoformat()
        if assignment.delivered_at
        else None,
        "processing_deadline_at": (
            assignment.opened_at
            + timedelta(seconds=lesson.settings["card_processing_deadline_sec"])
        ).isoformat()
        if assignment.opened_at
        else None,
    }


async def own_assignment(
    db: AsyncSession, assignment_id: UUID, student_id: UUID, lock: bool = False
) -> tuple[Assignment, Lesson]:
    query = select(Assignment).where(
        Assignment.id == assignment_id,
        Assignment.student_id == student_id,
        Assignment.state != "QUEUED",
    )
    lesson_query = select(Lesson).where(
        Lesson.id == query.with_only_columns(Assignment.lesson_id).scalar_subquery()
    )
    if lock:
        # Parallel students may edit different cards; finish/clock require an exclusive lock.
        lesson_query = lesson_query.with_for_update(read=True)
    lesson = await db.scalar(lesson_query)
    if lesson is None:
        raise APIError(404, "NOT_FOUND", "Карточка не найдена.")
    if lock:
        assignment = (
            await db.scalars(query.with_for_update().execution_options(populate_existing=True))
        ).one()
    else:
        assignment = (await db.scalars(query)).one()
    return assignment, lesson


async def my_block(db: AsyncSession, assignment: Assignment, user: User) -> dict[str, Any]:
    events = list(
        await db.scalars(
            select(StatusEvent)
            .where(StatusEvent.assignment_id == assignment.id)
            .order_by(StatusEvent.created_at, StatusEvent.id)
        )
    )
    service = await db.get(Service, user.service_id) if user.service_id else None
    return block_from_events(assignment, service, events)


def block_from_events(
    assignment: Assignment, service: Service | None, events: Sequence[StatusEvent]
) -> dict[str, Any]:
    manual = [event for event in events if not event.is_automatic]
    current = ResponseStatus(manual[-1].status) if manual else None
    available = (
        available_transitions(current)
        if assignment.state != "CLOSED" and assignment.task_mode != "CARD_ENTRY"
        else []
    )
    return {
        "service_code": service.code if service else None,
        "current_status": current,
        "available_statuses": [
            {
                "code": status,
                "label": STATUS_LABELS[status],
                "comment_required": status in {"NOT_ACCEPTED", "WORK_REFUSED"},
            }
            for status in available
        ],
        "history": [
            {
                "id": str(event.id),
                "status": event.status,
                "label": STATUS_LABELS[ResponseStatus(event.status)],
                "comment": event.comment,
                "is_automatic": event.is_automatic,
                "at": event.created_at.isoformat(),
                "elapsed_ms": event.elapsed_ms,
            }
            for event in events
        ],
    }


async def card_summary(
    db: AsyncSession, assignment: Assignment, lesson: Lesson, now: datetime
) -> dict[str, Any]:
    scenario = await db.get(Scenario, assignment.scenario_id)
    assert scenario is not None
    incident = await db.get(IncidentType, scenario.incident_type_id)
    assert incident is not None
    current = await db.scalar(
        select(StatusEvent.status)
        .where(StatusEvent.assignment_id == assignment.id, StatusEvent.is_automatic.is_(False))
        .order_by(StatusEvent.created_at.desc(), StatusEvent.id.desc())
        .limit(1)
    )
    return summarize_assignment(assignment, lesson, scenario, incident, current, now)


async def card_summaries(
    db: AsyncSession, assignments: list[Assignment], lesson: Lesson, now: datetime
) -> list[dict[str, Any]]:
    if not assignments:
        return []
    pairs = (
        await db.execute(
            select(Scenario, IncidentType)
            .join(IncidentType)
            .where(Scenario.id.in_({row.scenario_id for row in assignments}))
        )
    ).all()
    metadata = {scenario.id: (scenario, incident) for scenario, incident in pairs}
    statuses = {
        assignment_id: status
        for assignment_id, status in (
            await db.execute(
                select(StatusEvent.assignment_id, StatusEvent.status)
                .where(
                    StatusEvent.assignment_id.in_([row.id for row in assignments]),
                    StatusEvent.is_automatic.is_(False),
                )
                .distinct(StatusEvent.assignment_id)
                .order_by(
                    StatusEvent.assignment_id, StatusEvent.created_at.desc(), StatusEvent.id.desc()
                )
            )
        ).all()
    }
    return [
        summarize_assignment(row, lesson, *metadata[row.scenario_id], statuses.get(row.id), now)
        for row in assignments
    ]


def summarize_assignment(
    assignment: Assignment,
    lesson: Lesson,
    scenario: Scenario,
    incident: IncidentType,
    current: str | None,
    now: datetime,
) -> dict[str, Any]:
    deadline = (
        assignment.delivered_at + timedelta(seconds=lesson.settings["primary_status_deadline_sec"])
        if assignment.delivered_at
        else None
    )
    overdue = bool(
        deadline and (assignment.primary_status_at or assignment.closed_at or now) > deadline
    )
    if assignment.opened_at:
        processing_deadline = assignment.opened_at + timedelta(
            seconds=lesson.settings["card_processing_deadline_sec"]
        )
        overdue = overdue or (assignment.closed_at or now) > processing_deadline
    return {
        "assignment_id": str(assignment.id),
        "task_mode": assignment.task_mode,
        "card_number": assignment.card_number,
        "state": assignment.state,
        "origin": scenario.origin,
        "incident_type_name": "Входящее обращение"
        if assignment.task_mode == "CARD_ENTRY"
        else incident.name,
        "address_short": "Заполните по обращению"
        if assignment.task_mode == "CARD_ENTRY"
        else scenario.card_payload["address"]["raw"],
        "registered_at": scenario.card_payload["registered_at"],
        "delivered_at": assignment.delivered_at.isoformat() if assignment.delivered_at else None,
        **{
            key: value
            for key, value in timers(assignment, lesson, now).items()
            if key != "server_time"
        },
        "current_status": current,
        "is_overdue": overdue,
        "has_unread_description": assignment.opened_at is None,
    }


async def full_card(
    db: AsyncSession, assignment: Assignment, lesson: Lesson, user: User, now: datetime
) -> dict[str, Any]:
    scenario, incident = (
        await db.execute(
            select(Scenario, IncidentType)
            .join(IncidentType)
            .where(Scenario.id == assignment.scenario_id)
        )
    ).one()
    payload = scenario.card_payload
    entry = assignment.task_mode == "CARD_ENTRY"
    if entry:
        payload = (
            assignment.card_submission
            or assignment.card_draft
            or EntryCardInput().model_dump(mode="json")
        )
        chosen = (
            await db.get(IncidentType, UUID(payload["incident_type_id"]))
            if payload.get("incident_type_id")
            else None
        )
        payload = {
            **payload,
            "registered_at": (assignment.delivered_at or now).isoformat(),
            "attributes": list(chosen.attributes.values()) if chosen else [],
            "operator_workstation": "Учебный оператор",
        }
    # Whitelist student fields: reference and author metadata never cross this boundary.
    card = {
        key: payload.get(key)
        for key in (
            "registered_at",
            "operator_workstation",
            "applicant",
            "address",
            "attributes",
            "description",
        )
    }
    services = list(
        await db.scalars(
            select(Service)
            .where(
                or_(
                    and_(
                        Service.code.in_(payload.get("notified_services", [])),
                        Service.is_visible.is_(True),
                    ),
                    Service.id == user.service_id,
                )
            )
            .order_by(Service.sort_order, Service.code)
        )
    )
    own = next((service for service in services if service.id == user.service_id), None)
    events = list(
        await db.scalars(
            select(StatusEvent)
            .where(StatusEvent.assignment_id == assignment.id)
            .order_by(StatusEvent.created_at, StatusEvent.id)
        )
    )
    block = block_from_events(assignment, own, events)
    services.sort(key=lambda service: service.id != user.service_id)
    card.update(
        {
            "card_number": assignment.card_number,
            "origin": scenario.origin,
            "incident_type_name": (chosen.name if chosen else "Входящее обращение")
            if entry
            else incident.name,
            "modifiers": [
                {"code": code, "label": MODIFIER_LABELS[code]}
                for code in payload.get("modifiers", [])
            ],
            "notification_list": [
                {
                    "service_code": service.code,
                    "service_name": service.name,
                    "is_own": service.id == user.service_id,
                    "last_status": {
                        "status": block["current_status"],
                        "at": block["history"][-1]["at"],
                        "by": user.last_name,
                    }
                    if service.id == user.service_id and block["current_status"]
                    else None,
                }
                for service in services
            ],
        }
    )
    if scenario.origin == "EXTERNAL_SYSTEM":
        card.update(operator_workstation=None, attributes=[], modifiers=[])
    return {
        "assignment_id": str(assignment.id),
        "task_mode": assignment.task_mode,
        "state": assignment.state,
        "card": card,
        "my_block": block,
        "timers": timers(assignment, lesson, now),
        **(
            {
                "entry": {
                    "incoming_channel": lesson.settings.get("incoming_channel", "TEXT"),
                    "incoming_message": incoming_message(scenario.card_payload)
                    if assignment.opened_at or lesson.settings.get("incoming_channel") != "VOICE"
                    else "",
                    "draft": assignment.card_submission
                    or assignment.card_draft
                    or EntryCardInput().model_dump(mode="json"),
                    "revision": assignment.draft_revision,
                    "accepted_delay_ms": round(
                        (assignment.opened_at - assignment.delivered_at).total_seconds() * 1000
                    )
                    if assignment.opened_at and assignment.delivered_at
                    else None,
                    "submitted_at": assignment.submitted_at.isoformat()
                    if assignment.submitted_at
                    else None,
                    "score": assignment.score if assignment.state == "CLOSED" else None,
                }
            }
            if entry
            else {}
        ),
    }
