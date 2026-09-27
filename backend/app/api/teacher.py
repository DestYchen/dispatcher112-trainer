from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, Teacher
from app.api.errors import APIError
from app.db.models import (
    Assignment,
    AuditLog,
    GenerationJob,
    Lesson,
    LessonParticipant,
    Scenario,
    User,
    Workstation,
)
from app.domain.card_entry import incoming_message
from app.domain.cards import lesson_payload
from app.domain.lesson_settings import LessonSettings
from app.domain.pagination import next_cursor, page_offset
from app.domain.sip import ROOT, prepare_speech
from app.realtime.hub import hub
from app.scoring.service import score_assignment

router = APIRouter(prefix="/teacher", tags=["teacher"])


@router.get("/lessons")
async def lessons(
    db: DB,
    user: Teacher,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict[str, object]:
    offset = page_offset(cursor)
    rows = list(
        await db.scalars(
            select(Lesson)
            .where(Lesson.teacher_id == user.id)
            .order_by(Lesson.created_at.desc(), Lesson.id)
            .offset(offset)
            .limit(limit + 1)
        )
    )
    return {
        "items": [lesson_payload(row) for row in rows[:limit]],
        "next_cursor": next_cursor(offset, limit, len(rows) > limit),
    }


class ParticipantInput(BaseModel):
    student_id: UUID
    workstation_id: UUID | None = None


class LessonInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    settings: LessonSettings = Field(default_factory=LessonSettings)
    participants: list[ParticipantInput] = Field(min_length=1, max_length=100)


class AssignmentInput(BaseModel):
    student_id: UUID
    scenario_id: UUID
    task_mode: Literal["CARD_ACTIONS", "CARD_ENTRY"] | None = None


class AssignInput(BaseModel):
    assignments: list[AssignmentInput] = Field(min_length=1, max_length=1000)


async def own_lesson(db: AsyncSession, lesson_id: UUID, user: User, lock: bool = True) -> Lesson:
    query = select(Lesson).where(Lesson.id == lesson_id, Lesson.teacher_id == user.id)
    lesson = await db.scalar(query.with_for_update() if lock else query)
    if lesson is None:
        raise APIError(404, "NOT_FOUND", "Занятие не найдено.")
    return lesson


@router.get("/participants")
async def participants(db: DB, user: Teacher) -> dict[str, Any]:
    students = await db.scalars(
        select(User)
        .where(User.role == "STUDENT", User.is_active.is_(True))
        .order_by(User.last_name, User.login)
    )
    stations = await db.scalars(
        select(Workstation).where(Workstation.is_active.is_(True)).order_by(Workstation.number)
    )
    return {
        "students": [
            {"id": str(row.id), "name": f"{row.last_name} {row.first_name}"} for row in students
        ],
        "workstations": [{"id": str(row.id), "number": row.number} for row in stations],
    }


@router.get("/lessons/{lesson_id}")
async def lesson_detail(lesson_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user)
    participants = list(
        await db.scalars(select(LessonParticipant).where(LessonParticipant.lesson_id == lesson.id))
    )
    return {
        **lesson_payload(lesson),
        "participants": [
            {
                "student_id": str(row.student_id),
                "workstation_id": str(row.workstation_id) if row.workstation_id else None,
            }
            for row in participants
        ],
    }


@router.post("/lessons", status_code=201)
async def create_lesson(body: LessonInput, db: DB, user: Teacher) -> dict[str, Any]:
    ids = [row.student_id for row in body.participants]
    stations = [row.workstation_id for row in body.participants if row.workstation_id]
    if len(set(ids)) != len(ids) or len(set(stations)) != len(stations):
        raise APIError(400, "VALIDATION_ERROR", "Участники и рабочие места не должны повторяться.")
    for participant in body.participants:
        student = await db.get(User, participant.student_id)
        workstation = (
            await db.get(Workstation, participant.workstation_id)
            if participant.workstation_id
            else None
        )
        if not student or student.role != "STUDENT" or not student.is_active:
            raise APIError(400, "VALIDATION_ERROR", "Выберите действующего обучающегося.")
        if participant.workstation_id and (not workstation or not workstation.is_active):
            raise APIError(400, "VALIDATION_ERROR", "Рабочее место недоступно.")
    lesson = Lesson(
        title=body.title, teacher_id=user.id, settings=body.settings.model_dump(mode="json")
    )
    db.add(lesson)
    await db.flush()
    for participant in body.participants:
        db.add(LessonParticipant(lesson_id=lesson.id, **participant.model_dump()))
    db.add(
        AuditLog(
            user_id=user.id, action="LESSON_CREATED", entity_type="lesson", entity_id=lesson.id
        )
    )
    await db.commit()
    return lesson_payload(lesson)


@router.post("/lessons/{lesson_id}/assign")
async def assign(lesson_id: UUID, body: AssignInput, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user)
    if lesson.status == "FINISHED":
        raise APIError(409, "LESSON_NOT_RUNNING", "Занятие уже завершено.")
    members = set(
        await db.scalars(
            select(LessonParticipant.student_id).where(LessonParticipant.lesson_id == lesson.id)
        )
    )
    now = datetime.now(UTC)
    created = []
    for item in body.assignments:
        training_mode = lesson.settings.get("training_mode", "CARD_ACTIONS")
        task_mode = item.task_mode or (
            "CARD_ENTRY" if training_mode == "CARD_ENTRY" else "CARD_ACTIONS"
        )
        if training_mode != "MIXED" and task_mode != training_mode:
            raise APIError(
                400, "VALIDATION_ERROR", "Вид задания должен соответствовать режиму занятия."
            )
        scenario = await db.scalar(
            select(Scenario).where(Scenario.id == item.scenario_id).with_for_update(read=True)
        )
        if (
            item.student_id not in members
            or not scenario
            or scenario.status != "APPROVED"
            or scenario.card_payload.get("archived")
        ):
            raise APIError(
                400, "VALIDATION_ERROR", "Нужны участник занятия и утверждённый сценарий."
            )
        assignment_id = uuid4()
        number = f"{now:%Y-%m%d}-{assignment_id.int % 1000000:06d}"
        while await db.scalar(
            select(Assignment.id).where(
                Assignment.lesson_id == lesson.id, Assignment.card_number == number
            )
        ):
            number = f"{now:%Y-%m%d}-{uuid4().int % 1000000:06d}"
        row = Assignment(
            id=assignment_id,
            lesson_id=lesson.id,
            student_id=item.student_id,
            scenario_id=item.scenario_id,
            card_number=number,
            task_mode=task_mode,
        )
        db.add(row)
        await db.flush()
        created.append(str(row.id))
    db.add(
        AuditLog(
            user_id=user.id,
            action="ASSIGNMENTS_QUEUED",
            entity_type="lesson",
            entity_id=lesson.id,
            payload={"assignment_ids": created},
        )
    )
    await db.commit()
    return {"assignment_ids": created}


@router.post("/lessons/{lesson_id}/start")
async def start(lesson_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user)
    if lesson.status != "PLANNED":
        raise APIError(409, "LESSON_NOT_RUNNING", "Запустить можно только запланированное занятие.")
    members = list(
        await db.scalars(
            select(LessonParticipant)
            .where(LessonParticipant.lesson_id == lesson.id)
            .order_by(LessonParticipant.student_id)
        )
    )
    await db.scalars(
        select(User)
        .where(User.id.in_([row.student_id for row in members]))
        .order_by(User.id)
        .with_for_update()
    )
    occupied = await db.scalar(
        select(LessonParticipant.id)
        .join(Lesson)
        .where(
            LessonParticipant.student_id.in_([row.student_id for row in members]),
            Lesson.status == "RUNNING",
        )
        .limit(1)
    )
    if occupied:
        raise APIError(400, "VALIDATION_ERROR", "Участник уже находится в другом занятии.")
    queued = await db.scalar(
        select(Assignment.id)
        .where(Assignment.lesson_id == lesson.id, Assignment.state == "QUEUED")
        .limit(1)
    )
    if queued is None:
        raise APIError(400, "VALIDATION_ERROR", "Сначала назначьте утверждённые сценарии.")
    generating = await db.scalar(
        select(GenerationJob.id)
        .where(
            GenerationJob.lesson_id == lesson.id, GenerationJob.status.in_(["QUEUED", "RUNNING"])
        )
        .limit(1)
    )
    if generating:
        raise APIError(400, "VALIDATION_ERROR", "Дождитесь завершения генерации сценариев.")
    if lesson.settings.get("incoming_channel") == "VOICE":
        voice_scenarios = await db.scalars(
            select(Scenario)
            .join(Assignment)
            .where(Assignment.lesson_id == lesson.id, Assignment.task_mode == "CARD_ENTRY")
            .distinct()
        )
        missing_voice = False
        for scenario in voice_scenarios:
            name = prepare_speech(incoming_message(scenario.card_payload))
            missing_voice |= not (ROOT / "speech" / f"{name}.wav").is_file()
        if missing_voice:
            raise APIError(
                409,
                "VOICE_NOT_READY",
                "Подготавливаются голосовые реплики. Повторите запуск через несколько секунд.",
            )
    now = datetime.now(UTC)
    lesson.status, lesson.started_at = "RUNNING", now
    for member in members:
        member.joined_at = now
    db.add(
        AuditLog(
            user_id=user.id,
            action="LESSON_STARTED",
            entity_type="lesson",
            entity_id=lesson.id,
            created_at=now,
        )
    )
    await db.commit()
    for member in members:
        await hub.send(
            f"user:{member.student_id}", "LESSON_STARTED", {"lesson": lesson_payload(lesson)}, now
        )
    return lesson_payload(lesson)


@router.post("/lessons/{lesson_id}/finish")
async def finish(lesson_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user)
    if lesson.status != "RUNNING":
        raise APIError(409, "LESSON_NOT_RUNNING", "Занятие не идёт.")
    now = datetime.now(UTC)
    rows = list(
        await db.scalars(
            select(Assignment)
            .where(Assignment.lesson_id == lesson.id, Assignment.state.not_in(["CLOSED", "QUEUED"]))
            .with_for_update()
        )
    )
    for row in rows:
        row.state, row.closed_at = "CLOSED", now
        await score_assignment(db, row, lesson)
        db.add(
            AuditLog(
                user_id=user.id,
                action="CARD_CLOSED_BY_TEACHER",
                entity_type="assignment",
                entity_id=row.id,
                created_at=now,
            )
        )
    lesson.status, lesson.finished_at = "FINISHED", now
    members = list(
        await db.scalars(
            select(LessonParticipant.student_id).where(LessonParticipant.lesson_id == lesson.id)
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="LESSON_FINISHED",
            entity_type="lesson",
            entity_id=lesson.id,
            created_at=now,
        )
    )
    await db.commit()
    for student_id in members:
        await hub.send(f"user:{student_id}", "LESSON_FINISHED", {"lesson_id": str(lesson.id)}, now)
    return lesson_payload(lesson)
