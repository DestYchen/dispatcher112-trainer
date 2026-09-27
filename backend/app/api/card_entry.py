from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DB, Student, TrainingUser
from app.api.errors import APIError
from app.db.models import AuditLog, IncidentGroup, IncidentType, InteractionEvent, Service
from app.domain.card_entry import EntryCardInput
from app.domain.cards import own_assignment
from app.domain.classifier import resolve_services
from app.domain.enums import MODIFIER_LABELS
from app.domain.idempotency import previous_response, save_response
from app.realtime.hub import hub
from app.scoring.service import score_assignment

router = APIRouter(prefix="/student", tags=["card-entry"])


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    card: EntryCardInput


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


@router.get("/entry-directory")
async def entry_directory(db: DB, user: TrainingUser) -> dict[str, Any]:
    groups = await db.scalars(
        select(IncidentGroup).order_by(IncidentGroup.sort_order, IncidentGroup.code)
    )
    services = await db.scalars(
        select(Service)
        .where(Service.is_visible.is_(True))
        .order_by(Service.sort_order, Service.code)
    )
    return {
        "groups": [{"id": str(row.id), "name": row.name} for row in groups],
        "services": [{"code": row.code, "name": row.name} for row in services],
        "modifiers": [{"code": code, "label": label} for code, label in MODIFIER_LABELS.items()],
    }


@router.get("/entry-types")
async def entry_types(
    db: DB, user: TrainingUser, group_id: UUID, q: str = Query(default="", max_length=200)
) -> dict[str, Any]:
    query = select(IncidentType).where(IncidentType.group_id == group_id)
    if q.strip():
        query = query.where(IncidentType.name.icontains(q.strip(), autoescape=True))
    rows = await db.scalars(query.order_by(IncidentType.code).limit(200))
    return {
        "items": [
            {"id": str(row.id), "name": row.name, "attributes": row.attributes} for row in rows
        ]
    }


@router.get("/entry-services")
async def entry_services(
    db: DB,
    user: TrainingUser,
    incident_type_id: UUID,
    modifiers: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    modifiers = modifiers or []
    if len(modifiers) > 6 or any(code not in MODIFIER_LABELS for code in modifiers):
        raise APIError(400, "VALIDATION_ERROR", "Неизвестный модификатор.")
    if await db.get(IncidentType, incident_type_id) is None:
        raise APIError(404, "NOT_FOUND", "Тип происшествия не найден.")
    services = await resolve_services(db, incident_type_id, modifiers)
    return {"service_codes": [row.code for row in services]}


@router.get("/entry-types/{incident_type_id}")
async def entry_type(incident_type_id: UUID, db: DB, user: TrainingUser) -> dict[str, Any]:
    row = await db.get(IncidentType, incident_type_id)
    if row is None:
        raise APIError(404, "NOT_FOUND", "Тип происшествия не найден.")
    return {
        "id": str(row.id),
        "group_id": str(row.group_id),
        "name": row.name,
        "attributes": row.attributes,
    }


@router.post("/assignments/{assignment_id}/draft")
async def save_draft(
    assignment_id: UUID,
    body: DraftInput,
    db: DB,
    user: Student,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    now = datetime.now(UTC)
    original = body.model_dump(mode="json")
    previous = await previous_response(
        db, user.id, assignment.id, "draft", idempotency_key, original, now
    )
    if previous is not None:
        return previous
    if assignment.state == "CLOSED":
        raise APIError(409, "CARD_CLOSED", "Карточка закрыта для редактирования.")
    if lesson.status != "RUNNING" or assignment.opened_at is None:
        raise APIError(409, "LESSON_NOT_RUNNING", "Примите обращение в текущем занятии.")
    if assignment.task_mode != "CARD_ENTRY":
        raise APIError(
            400, "VALIDATION_ERROR", "Упражнение не предусматривает заполнение карточки."
        )
    if body.revision != assignment.draft_revision:
        raise APIError(
            409,
            "DRAFT_CONFLICT",
            "Черновик уже изменён в другой вкладке. Загрузите актуальную версию.",
        )
    card = body.card
    if card.incident_type_id and await db.get(IncidentType, card.incident_type_id) is None:
        raise APIError(400, "VALIDATION_ERROR", "Выберите тип из справочника.")
    if any(code not in MODIFIER_LABELS for code in card.modifiers) or len(
        set(card.modifiers)
    ) != len(card.modifiers):
        raise APIError(400, "VALIDATION_ERROR", "Проверьте особые признаки.")
    visible = set(await db.scalars(select(Service.code).where(Service.is_visible.is_(True))))
    if set(card.notified_services) - visible or len(set(card.notified_services)) != len(
        card.notified_services
    ):
        raise APIError(400, "VALIDATION_ERROR", "Выберите службы из справочника без повторений.")
    assignment.card_draft = card.model_dump(mode="json")
    assignment.draft_revision += 1
    payload = {"revision": assignment.draft_revision, "card": assignment.card_draft}
    db.add(
        InteractionEvent(
            assignment_id=assignment.id, kind="CARD_DRAFT_SAVED", payload=payload, created_at=now
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="CARD_DRAFT_SAVED",
            entity_type="assignment",
            entity_id=assignment.id,
            payload=payload,
            created_at=now,
        )
    )
    result = {
        "draft": assignment.card_draft,
        "revision": assignment.draft_revision,
        "server_time": now.isoformat(),
    }
    save_response(db, user.id, assignment.id, "draft", idempotency_key, original, result, now)
    await db.commit()
    await hub.send(
        f"user:{lesson.teacher_id}",
        "STUDENT_ACTION",
        {
            "student_id": str(user.id),
            "assignment_id": str(assignment.id),
            "kind": "CARD_DRAFT_SAVED",
            "status": None,
        },
        now,
    )
    return result


@router.post("/assignments/{assignment_id}/submit-card")
async def submit_card(
    assignment_id: UUID,
    body: SubmitInput,
    db: DB,
    user: Student,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    now = datetime.now(UTC)
    original = body.model_dump(mode="json")
    previous = await previous_response(
        db, user.id, assignment.id, "submit-card", idempotency_key, original, now
    )
    if previous is not None:
        return previous
    if assignment.state == "CLOSED":
        raise APIError(409, "CARD_CLOSED", "Карточка уже закрыта.")
    if lesson.status != "RUNNING" or assignment.opened_at is None:
        raise APIError(409, "LESSON_NOT_RUNNING", "Примите обращение в текущем занятии.")
    if assignment.task_mode != "CARD_ENTRY" or assignment.card_draft is None:
        raise APIError(400, "VALIDATION_ERROR", "Сначала сохраните заполненную карточку.")
    if body.revision != assignment.draft_revision:
        raise APIError(409, "DRAFT_CONFLICT", "Сдайте актуальную версию черновика.")
    assignment.card_submission = dict(assignment.card_draft)
    assignment.submitted_at = assignment.closed_at = now
    assignment.state = "CLOSED"
    score = await score_assignment(db, assignment, lesson)
    db.add(
        InteractionEvent(
            assignment_id=assignment.id,
            kind="CARD_SUBMITTED",
            payload={"revision": body.revision},
            created_at=now,
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="CARD_SUBMITTED",
            entity_type="assignment",
            entity_id=assignment.id,
            payload={"revision": body.revision, "total": score["total"]},
            created_at=now,
        )
    )
    result = {"state": "CLOSED", "submitted_at": now.isoformat(), "score": score}
    save_response(db, user.id, assignment.id, "submit-card", idempotency_key, original, result, now)
    await db.commit()
    payload = {
        "student_id": str(user.id),
        "assignment_id": str(assignment.id),
        "kind": "CARD_SUBMITTED",
        "status": None,
    }
    await hub.send(f"user:{lesson.teacher_id}", "STUDENT_ACTION", payload, now)
    await hub.send(f"user:{user.id}", "CARD_CLOSED", payload, now)
    return result
