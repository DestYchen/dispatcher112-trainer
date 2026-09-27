import asyncio
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import IncidentType, LessonParticipant, Scenario, Service, Street, User
from app.domain.classifier import resolve_services
from app.domain.enums import MODIFIER_LABELS
from app.generation.llm import LocalLLMGenerator, TemplateGenerator, TextGenerator
from app.generation.validation import validate_scenario


def build_reference(service_codes: list[str], own_service_code: str | None) -> dict[str, Any]:
    own = own_service_code in service_codes
    return {
        "expected_status": "ACCEPTED" if own else "NOT_ACCEPTED",
        "expected_status_chain": [
            "ACCEPTED",
            "RESPONSE_STARTED",
            "ARRIVED",
            "WORK_IN_PROGRESS",
            "WORK_COMPLETED",
        ]
        if own
        else ["NOT_ACCEPTED"],
        "comment_required": not own,
        "comment_must_contain": [] if own else ["reason", "handed_to"],
        "report_required": own,
        "report_callee_code": "DUTY_OFFICER" if own else None,
        "report_must_mention": ["address", "incident_type", "victims"] if own else [],
        "rationale": "Профильное происшествие для службы участника."
        if own
        else "Происшествие относится к другой службе; обоснуйте передачу информации.",
        "trap": None if own else "NOT_OUR_SERVICE",
    }


async def build_scenario(
    db: AsyncSession,
    lesson_id: UUID,
    author_id: UUID,
    request: dict[str, Any],
    seed: int,
    generator: TextGenerator | None = None,
) -> Scenario:
    rng = random.Random(seed)
    groups = [UUID(value) for value in request.get("incident_group_ids", [])]
    low, high = request.get("difficulty_range", [1, 6])
    query = select(IncidentType).where(IncidentType.difficulty.between(low, high))
    if groups:
        query = query.where(IncidentType.group_id.in_(groups))
    types = list(await db.scalars(query.order_by(IncidentType.code)))
    street_query = select(Street)
    if request.get("street_ids"):
        street_query = street_query.where(
            Street.id.in_([UUID(value) for value in request["street_ids"]])
        )
    streets = list(await db.scalars(street_query.order_by(Street.name_norm)))
    if not types or not streets:
        raise ValueError("Нет типов или улиц для выбранных параметров генерации.")
    incident, street = rng.choice(types), rng.choice(streets)
    own_codes = set(
        await db.scalars(
            select(Service.code)
            .join(User, User.service_id == Service.id)
            .join(LessonParticipant, LessonParticipant.student_id == User.id)
            .where(LessonParticipant.lesson_id == lesson_id)
        )
    )
    if len(own_codes) != 1:
        raise ValueError("Для одного эталона участники должны относиться к одной службе.")
    origin = (
        "OPERATOR_112"
        if rng.random() < request.get("origin_mix", {}).get("OPERATOR_112", 0.8)
        else "EXTERNAL_SYSTEM"
    )
    modifiers = rng.sample(
        ["THREAT_TO_PEOPLE", "VICTIMS", "NO_ACCESS", "ROAD_BLOCKED", "CHILD_INVOLVED"],
        min(2, incident.difficulty // 4),
    )
    services = await resolve_services(db, incident.id, modifiers)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "registered_at": now.isoformat(),
        "operator_workstation": f"ОП-{rng.randrange(1, 100):03d}"
        if origin == "OPERATOR_112"
        else None,
        "applicant": {
            "name": rng.choice(
                ["Учебный заявитель Анна", "Учебный заявитель Пётр", "Учебный заявитель Мария"]
            ),
            "phone": f"+7 000 ***-**-{rng.randrange(100):02d}",
        },
        "address": {
            "raw": f"{street.name}, д. {rng.randrange(1, 100)}",
            "clarification": "Учебный адрес",
            "lat": None,
            "lon": None,
        },
        "attributes": list(incident.attributes.values()) if origin == "OPERATOR_112" else [],
        "incident_type_name": incident.name,
        "modifiers": modifiers if origin == "OPERATOR_112" else [],
        "notified_services": [service.code for service in services],
    }
    if generator is None:
        generator = (
            TemplateGenerator()
            if (request.get("generation_backend") or settings.generation_backend) == "template"
            else LocalLLMGenerator()
        )
    prompt = json.dumps(
        {
            "incident_type_name": incident.name,
            "modifiers": modifiers,
            "modifier_facts": [MODIFIER_LABELS[modifier] for modifier in modifiers],
            "review_comment": request.get("review_comment", ""),
        },
        ensure_ascii=False,
    )
    for _ in range(3):
        payload["description"] = await generator.generate(prompt)
        if 40 <= len(payload["description"]) <= 400:
            break
    validation = await validate_scenario(db, payload, incident, origin)
    payload["validation"] = validation
    payload["generation"] = {
        "backend": request.get("generation_backend") or settings.generation_backend,
        "model": settings.local_llm_model if isinstance(generator, LocalLLMGenerator) else None,
        "seed": str(seed),
    }
    if isinstance(generator, LocalLLMGenerator):
        manifest_path = Path("/data/llm/manifest.json")
        if await asyncio.to_thread(manifest_path.is_file):
            manifest = json.loads(
                await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
            )
            if manifest.get("model") == settings.local_llm_model:
                payload["generation"]["manifest_sha256"] = manifest["manifest_sha256"]
    payload["lesson_id"] = str(lesson_id)
    rejected = any(
        not row["ok"] and row["action"] in {"REJECT", "REGENERATE"} for row in validation
    )
    return Scenario(
        title=f"{incident.name} · {street.name}",
        source="GENERATED",
        status="REJECTED" if rejected else "PENDING_REVIEW",
        origin=origin,
        incident_type_id=incident.id,
        difficulty=incident.difficulty,
        card_payload=payload,
        reference=build_reference(payload["notified_services"], next(iter(own_codes))),
        author_id=author_id,
        review_comment=request.get("review_comment"),
    )
