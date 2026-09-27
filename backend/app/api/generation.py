import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select

from app.api.deps import DB, Teacher
from app.api.errors import APIError
from app.api.teacher import own_lesson
from app.db.models import (
    Assignment,
    AuditLog,
    GenerationJob,
    IncidentGroup,
    IncidentType,
    LearningModule,
    Lesson,
    Scenario,
    Service,
    Street,
)
from app.domain.card_entry import EntryCardInput
from app.domain.enums import MODIFIER_LABELS, ResponseStatus
from app.domain.pagination import next_cursor, page_offset
from app.domain.status_machine import InvalidTransition, validate_transition
from app.generation.jobs import enqueue_generation
from app.generation.validation import validate_scenario

router = APIRouter(prefix="/teacher", tags=["generation"])


class GenerationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_group_ids: list[UUID] = Field(default_factory=list)
    difficulty_range: tuple[int, int] = (1, 6)
    count: int = Field(default=40, ge=1, le=200)
    generation_backend: Literal["template", "local_llm"] | None = None
    street_ids: list[UUID] = Field(default_factory=list, max_length=100)
    origin_mix: dict[str, float] = Field(
        default_factory=lambda: {"OPERATOR_112": 0.8, "EXTERNAL_SYSTEM": 0.2}
    )

    @model_validator(mode="after")
    def valid_parameters(self) -> Self:
        if not 1 <= self.difficulty_range[0] <= self.difficulty_range[1] <= 10:
            raise ValueError("Неверный диапазон сложности.")
        if (
            set(self.origin_mix) != {"OPERATOR_112", "EXTERNAL_SYSTEM"}
            or any(value < 0 or value > 1 for value in self.origin_mix.values())
            or abs(sum(self.origin_mix.values()) - 1) > 0.000001
        ):
            raise ValueError("Доли источников должны давать единицу.")
        return self


class ReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["ACCEPTED", "NOT_ACCEPTED"]
    expected_status_chain: list[ResponseStatus] = Field(min_length=1)
    comment_required: bool
    comment_must_contain: list[Literal["reason", "handed_to", "card_number", "clarified_address"]]
    report_required: bool
    report_callee_code: str | None
    report_must_mention: list[Literal["address", "incident_type", "victims"]]
    rationale: str = Field(min_length=1, max_length=2000)
    entry_description_keywords: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=50
    )
    trap: (
        Literal["DUPLICATE_CARD", "NOT_OUR_SERVICE", "DETAIL_IN_DESCRIPTION", "AMBIGUOUS_ADDRESS"]
        | None
    )

    @model_validator(mode="after")
    def valid_chain(self) -> Self:
        if self.expected_status_chain[0] != self.expected_status:
            raise ValueError("Цепочка должна начинаться с первичного статуса.")
        current = None
        for status in self.expected_status_chain:
            try:
                validate_transition(current, status, "Эталонный комментарий преподавателя")
            except InvalidTransition as error:
                raise ValueError("В эталоне недопустимая цепочка статусов.") from error
            current = status
        if self.report_required and not self.report_callee_code:
            raise ValueError("Укажите получателя доклада.")
        return self


class ApproveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    difficulty: int = Field(ge=1, le=10)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    reference: ReferenceInput | None = None
    description: str | None = Field(default=None, min_length=40, max_length=400)
    review_comment: str | None = Field(default=None, max_length=2000)
    incoming_message: str | None = Field(default=None, min_length=1, max_length=10000)
    card: EntryCardInput | None = None


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    comment: str = Field(min_length=1, max_length=2000)
    regenerate: bool = True


@router.get("/incident-groups")
async def incident_groups(db: DB, user: Teacher) -> dict[str, Any]:
    rows = await db.scalars(
        select(IncidentGroup).order_by(IncidentGroup.sort_order, IncidentGroup.code)
    )
    return {"items": [{"id": str(row.id), "name": row.name} for row in rows], "next_cursor": None}


@router.get("/generation/locations")
async def generation_locations(db: DB, user: Teacher) -> dict[str, Any]:
    rows = await db.scalars(select(Street).order_by(Street.name_norm))
    return {
        "items": [{"id": str(row.id), "name": row.name, "district": row.district} for row in rows]
    }


@router.post("/lessons/{lesson_id}/scenarios/generate", status_code=202)
async def generate(lesson_id: UUID, body: GenerationInput, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user)
    if lesson.status != "PLANNED":
        raise APIError(
            409, "LESSON_NOT_RUNNING", "Подготовка сценариев доступна до начала занятия."
        )
    for group_id in body.incident_group_ids:
        if await db.get(IncidentGroup, group_id) is None:
            raise APIError(400, "VALIDATION_ERROR", "Группа происшествий не найдена.")
    job = GenerationJob(lesson_id=lesson.id, user_id=user.id, request=body.model_dump(mode="json"))
    db.add(job)
    await db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            action="GENERATION_QUEUED",
            entity_type="generation_job",
            entity_id=job.id,
        )
    )
    await db.commit()
    await enqueue_generation(job.id)
    return {"job_id": str(job.id), "status": "QUEUED"}


@router.get("/jobs/{job_id}")
async def job_status(job_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    job = await db.scalar(
        select(GenerationJob).where(GenerationJob.id == job_id, GenerationJob.user_id == user.id)
    )
    if job is None:
        raise APIError(404, "NOT_FOUND", "Задача не найдена.")
    return {
        "job_id": str(job.id),
        "status": job.status,
        "progress": job.progress,
        "generated": job.generated,
        "rejected": job.rejected,
        "error": job.error,
    }


@router.get("/scenarios")
async def scenarios(
    db: DB,
    user: Teacher,
    status: Literal["DRAFT", "PENDING_REVIEW", "APPROVED", "REJECTED"] = "APPROVED",
    lesson_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    query = select(Scenario).where(
        Scenario.status == status, or_(Scenario.author_id == user.id, Scenario.status == "APPROVED")
    )
    if not include_archived:
        query = query.where(Scenario.card_payload["archived"].as_boolean().is_not(True))
    if lesson_id:
        await own_lesson(db, lesson_id, user)
        query = query.where(Scenario.card_payload["lesson_id"].astext == str(lesson_id))
    offset = page_offset(cursor)
    rows = list(
        await db.scalars(
            query.order_by(Scenario.created_at.desc(), Scenario.id).offset(offset).limit(limit + 1)
        )
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "title": row.title,
                "difficulty": row.difficulty,
                "incident_type_id": str(row.incident_type_id),
                "status": row.status,
                "archived": bool(row.card_payload.get("archived")),
                "editable": row.author_id == user.id,
                "card_payload": row.card_payload,
                "reference": row.reference,
                "validation": row.card_payload.get("validation", []),
                "review_comment": row.review_comment,
            }
            for row in rows[:limit]
        ],
        "next_cursor": next_cursor(offset, limit, len(rows) > limit),
    }


@router.post("/scenarios/{scenario_id}/copy", status_code=201)
async def copy_scenario(scenario_id: UUID, db: DB, user: Teacher) -> dict[str, str]:
    source = await db.scalar(
        select(Scenario).where(
            Scenario.id == scenario_id,
            or_(Scenario.author_id == user.id, Scenario.status == "APPROVED"),
        )
    )
    if source is None:
        raise APIError(404, "NOT_FOUND", "Сценарий не найден.")
    payload = {
        key: value
        for key, value in source.card_payload.items()
        if key not in {"archived", "lesson_id", "source_assignment_id"}
    }
    payload["revised_from"] = str(source.id)
    row = Scenario(
        id=uuid4(),
        title=source.title[:240] + " · новая версия",
        source="MANUAL",
        status="PENDING_REVIEW",
        origin=source.origin,
        incident_type_id=source.incident_type_id,
        difficulty=source.difficulty,
        card_payload=payload,
        reference=source.reference,
        author_id=user.id,
    )
    db.add(row)
    db.add(
        AuditLog(
            user_id=user.id,
            action="SCENARIO_COPIED",
            entity_type="scenario",
            entity_id=row.id,
            payload={"source_scenario_id": str(source.id)},
        )
    )
    await db.commit()
    return {"id": str(row.id)}


class ScenarioArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


@router.patch("/scenarios/{scenario_id}/archive")
async def archive_scenario(
    scenario_id: UUID, body: ScenarioArchiveInput, db: DB, user: Teacher
) -> dict[str, Any]:
    row = await db.scalar(
        select(Scenario)
        .where(Scenario.id == scenario_id, Scenario.author_id == user.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Сценарий не найден.")
    if bool(row.card_payload.get("archived")) != body.archived:
        row.card_payload = {**row.card_payload, "archived": body.archived}
        db.add(
            AuditLog(
                user_id=user.id,
                action="SCENARIO_ARCHIVED",
                entity_type="scenario",
                entity_id=row.id,
                payload=body.model_dump(),
            )
        )
        await db.commit()
    return {"id": str(row.id), "archived": body.archived}


@router.delete("/scenarios/{scenario_id}", status_code=204)
async def delete_scenario(scenario_id: UUID, db: DB, user: Teacher) -> None:
    row = await db.scalar(
        select(Scenario)
        .where(Scenario.id == scenario_id, Scenario.author_id == user.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Сценарий не найден.")
    used = await db.scalar(select(Assignment.id).where(Assignment.scenario_id == row.id).limit(1))
    module = await db.scalar(
        select(LearningModule.id)
        .where(LearningModule.scenario_ids.contains([str(row.id)]))
        .limit(1)
    )
    if used or module:
        raise APIError(
            409,
            "SCENARIO_IN_USE",
            "Сценарий использован в обучении. Уберите его из новых назначений вместо удаления.",
        )
    db.add(
        AuditLog(
            user_id=user.id,
            action="SCENARIO_DELETED",
            entity_type="scenario",
            entity_id=row.id,
            payload={"title": row.title, "source": row.source},
        )
    )
    await db.delete(row)
    await db.commit()


@router.post("/scenarios/{scenario_id}/approve")
async def approve(scenario_id: UUID, body: ApproveInput, db: DB, user: Teacher) -> dict[str, Any]:
    row = await db.scalar(
        select(Scenario)
        .where(Scenario.id == scenario_id, Scenario.author_id == user.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Сценарий не найден.")
    if row.status != "PENDING_REVIEW":
        raise APIError(400, "VALIDATION_ERROR", "Сценарий уже рассмотрен.")
    if body.title is not None:
        if not body.title.strip():
            raise APIError(400, "VALIDATION_ERROR", "Название не должно быть пустым.")
        row.title = body.title.strip()
    if body.card is not None:
        card = body.card
        if not card.incident_type_id or not (
            chosen := await db.get(IncidentType, card.incident_type_id)
        ):
            raise APIError(400, "VALIDATION_ERROR", "Выберите тип происшествия из справочника.")
        services = set(await db.scalars(select(Service.code)))
        if set(card.notified_services) - services or any(
            code not in MODIFIER_LABELS for code in card.modifiers
        ):
            raise APIError(400, "VALIDATION_ERROR", "Проверьте службы и признаки по справочнику.")
        row.incident_type_id = chosen.id
        row.card_payload = {
            **row.card_payload,
            **card.model_dump(mode="json"),
            "incident_type_name": chosen.name,
            "attributes": list(chosen.attributes.values()) if row.origin == "OPERATOR_112" else [],
        }
    if body.description is not None:
        row.card_payload = {**row.card_payload, "description": body.description}
    if body.incoming_message is not None:
        row.card_payload = {**row.card_payload, "incoming_message": body.incoming_message.strip()}
    incident = await db.get(IncidentType, row.incident_type_id)
    assert incident is not None
    validation = await validate_scenario(db, row.card_payload, incident, row.origin)
    if any(not check["ok"] for check in validation):
        raise APIError(
            400,
            "VALIDATION_ERROR",
            "Исправьте замечания автоматической проверки.",
            {"validation": validation},
        )
    row.card_payload = {**row.card_payload, "validation": validation}
    if body.reference:
        row.reference = body.reference.model_dump(mode="json", exclude_unset=True)
    if body.review_comment is not None:
        row.review_comment = body.review_comment.strip()
    row.status, row.difficulty, row.approved_by, row.approved_at = (
        "APPROVED",
        body.difficulty,
        user.id,
        datetime.now(UTC),
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="SCENARIO_APPROVED",
            entity_type="scenario",
            entity_id=row.id,
            payload={"reference": row.reference, "difficulty": row.difficulty},
        )
    )
    await db.commit()
    if row.review_comment:
        await asyncio.to_thread(
            append_feedback,
            {
                "scenario_id": str(row.id),
                "teacher_id": str(user.id),
                "comment": row.review_comment,
                "at": datetime.now(UTC).isoformat(),
            },
        )
    return {"id": str(row.id), "status": row.status}


@router.post("/assignments/{assignment_id}/reuse-card", status_code=201)
async def reuse_card(assignment_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    row = await db.scalar(
        select(Assignment)
        .join(Lesson)
        .where(Assignment.id == assignment_id, Lesson.teacher_id == user.id)
        .with_for_update(of=Assignment)
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Карточка не найдена.")
    if row.task_mode != "CARD_ENTRY" or row.card_submission is None or row.state != "CLOSED":
        raise APIError(400, "VALIDATION_ERROR", "Нужна сданная карточка обучающегося.")
    previous = await db.scalar(
        select(Scenario).where(
            Scenario.author_id == user.id,
            Scenario.card_payload["source_assignment_id"].astext == str(row.id),
        )
    )
    if previous is not None:
        return {"id": str(previous.id), "status": previous.status}
    source = await db.get(Scenario, row.scenario_id)
    assert source is not None
    card = row.card_submission
    incident = (
        await db.get(IncidentType, UUID(card["incident_type_id"]))
        if card.get("incident_type_id")
        else None
    )
    if incident is None:
        raise APIError(400, "VALIDATION_ERROR", "В сданной карточке не выбран тип происшествия.")
    payload = {
        **card,
        "card_number": row.card_number,
        "registered_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "operator_workstation": "Учебный оператор",
        "incident_type_name": incident.name,
        "attributes": list(incident.attributes.values()),
        "source_assignment_id": str(row.id),
    }
    payload["validation"] = await validate_scenario(db, payload, incident, "OPERATOR_112")
    scenario = Scenario(
        title=f"Карточка обучающегося {row.card_number}",
        source="MANUAL",
        status="PENDING_REVIEW",
        origin="OPERATOR_112",
        incident_type_id=incident.id,
        difficulty=source.difficulty,
        author_id=user.id,
        card_payload=payload,
        reference=dict(source.reference),
        review_comment="Проверьте карточку и эталон действий перед утверждением.",
    )
    db.add(scenario)
    await db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            action="STUDENT_CARD_REUSED",
            entity_type="scenario",
            entity_id=scenario.id,
            payload={"source_assignment_id": str(row.id)},
        )
    )
    await db.commit()
    return {"id": str(scenario.id), "status": scenario.status}


def append_feedback(entry: dict[str, Any]) -> None:
    with Path("/data/feedback.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(entry, ensure_ascii=False) + "\n")


@router.post("/scenarios/{scenario_id}/reject")
async def reject(scenario_id: UUID, body: RejectInput, db: DB, user: Teacher) -> dict[str, Any]:
    row = await db.scalar(
        select(Scenario)
        .where(Scenario.id == scenario_id, Scenario.author_id == user.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Сценарий не найден.")
    if row.status != "PENDING_REVIEW":
        raise APIError(400, "VALIDATION_ERROR", "Сценарий уже рассмотрен.")
    job = None
    if body.regenerate:
        lesson_id = row.card_payload.get("lesson_id")
        if not lesson_id:
            raise APIError(
                400, "VALIDATION_ERROR", "Сценарий не связан с занятием; отключите перегенерацию."
            )
        lesson = await own_lesson(db, UUID(lesson_id), user)
        if lesson.status != "PLANNED":
            raise APIError(409, "LESSON_NOT_RUNNING", "Перегенерация доступна до начала занятия.")
        incident = await db.get(IncidentType, row.incident_type_id)
        assert incident is not None
        request = GenerationInput(
            incident_group_ids=[incident.group_id],
            difficulty_range=(incident.difficulty, incident.difficulty),
            count=1,
        ).model_dump(mode="json")
        request["review_comment"] = body.comment
        job = GenerationJob(lesson_id=lesson.id, user_id=user.id, request=request)
        db.add(job)
    row.status, row.review_comment = "REJECTED", body.comment
    await db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            action="SCENARIO_REJECTED",
            entity_type="scenario",
            entity_id=row.id,
            payload={"comment": body.comment, "job_id": str(job.id) if job else None},
        )
    )
    entry = {
        "scenario_id": str(row.id),
        "teacher_id": str(user.id),
        "comment": body.comment,
        "at": datetime.now(UTC).isoformat(),
    }
    await db.commit()
    await asyncio.to_thread(append_feedback, entry)
    if job:
        await enqueue_generation(job.id)
    return {"id": str(row.id), "status": row.status, "job_id": str(job.id) if job else None}
