from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.api.deps import DB, Teacher
from app.api.errors import APIError
from app.api.sip import call_payload
from app.api.teacher import own_lesson
from app.db.models import (
    Assignment,
    AuditLog,
    InteractionEvent,
    Lesson,
    LessonParticipant,
    SipCall,
    User,
)
from app.domain.cards import card_summaries, full_card, lesson_payload
from app.domain.teacher_live import live_snapshot
from app.realtime.hub import hub
from app.scoring.effective import effective_score

router = APIRouter(prefix="/teacher", tags=["teacher-live"])
Axis = Literal["timeliness", "correctness", "completeness", "literacy"]
Score = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


@router.get("/lessons/{lesson_id}/live")
async def live(lesson_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    return await live_snapshot(db, lesson, datetime.now(UTC))


@router.get("/lessons/{lesson_id}/students/{student_id}/observe")
async def observe(lesson_id: UUID, student_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    member = await db.scalar(
        select(LessonParticipant.id).where(
            LessonParticipant.lesson_id == lesson.id, LessonParticipant.student_id == student_id
        )
    )
    if member is None:
        raise APIError(404, "NOT_FOUND", "Участник не найден.")
    now = datetime.now(UTC)
    rows = list(
        await db.scalars(
            select(Assignment)
            .where(
                Assignment.lesson_id == lesson.id,
                Assignment.student_id == student_id,
                Assignment.state != "QUEUED",
            )
            .order_by(Assignment.delivered_at, Assignment.id)
        )
    )
    return {
        "server_time": now.isoformat(),
        "lesson": lesson_payload(lesson),
        "cards": await card_summaries(db, rows, lesson, now),
    }


@router.get("/assignments/{assignment_id}")
async def observe_card(assignment_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    pair = (
        await db.execute(
            select(Assignment, Lesson)
            .join(Lesson)
            .where(
                Assignment.id == assignment_id,
                Lesson.teacher_id == user.id,
                Assignment.state != "QUEUED",
            )
        )
    ).first()
    if pair is None:
        raise APIError(404, "NOT_FOUND", "Карточка не найдена.")
    assignment, lesson = pair
    student = await db.get(User, assignment.student_id)
    assert student
    detail = await full_card(db, assignment, lesson, student, datetime.now(UTC))
    events = list(
        await db.scalars(
            select(InteractionEvent)
            .where(InteractionEvent.assignment_id == assignment.id)
            .order_by(InteractionEvent.created_at, InteractionEvent.id)
        )
    )
    return {
        **detail,
        "sip_calls": [
            call_payload(row)
            for row in await db.scalars(
                select(SipCall)
                .where(SipCall.assignment_id == assignment.id)
                .order_by(SipCall.created_at)
            )
        ],
        "score": assignment.score,
        "teacher_override": assignment.teacher_override,
        "effective_score": effective_score(assignment.score, assignment.teacher_override)
        if assignment.score
        else None,
        "events": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "payload": row.payload,
                "at": row.created_at.isoformat(),
            }
            for row in events
        ],
    }


class OverrideInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    axes: dict[Axis, Score] = Field(default_factory=dict)
    total: Score | None = None
    comment: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def contains_score(self) -> Self:
        if not self.axes and self.total is None:
            raise ValueError("Укажите ось или итоговую оценку.")
        return self


@router.post("/assignments/{assignment_id}/override")
async def override(
    assignment_id: UUID, body: OverrideInput, db: DB, user: Teacher
) -> dict[str, Any]:
    row = await db.scalar(
        select(Assignment)
        .join(Lesson)
        .where(Assignment.id == assignment_id, Lesson.teacher_id == user.id)
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Карточка не найдена.")
    lesson = await own_lesson(db, row.lesson_id, user)
    row = (
        await db.scalars(
            select(Assignment)
            .where(Assignment.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()
    if row.state != "CLOSED" or row.score is None:
        raise APIError(409, "VALIDATION_ERROR", "Сначала завершите обработку карточки.")
    previous = row.teacher_override
    now = datetime.now(UTC)
    row.teacher_override = {
        **body.model_dump(mode="json", exclude_none=True),
        "teacher_id": str(user.id),
        "at": now.isoformat(),
    }
    db.add(
        AuditLog(
            user_id=user.id,
            action="SCORE_OVERRIDE",
            entity_type="assignment",
            entity_id=row.id,
            payload={"previous": previous, "override": row.teacher_override},
        )
    )
    await db.commit()
    await hub.send(
        f"user:{lesson.teacher_id}",
        "STUDENT_ACTION",
        {
            "student_id": str(row.student_id),
            "assignment_id": str(row.id),
            "kind": "SCORE_OVERRIDE",
            "status": None,
        },
        now,
    )
    return {
        "score": row.score,
        "teacher_override": row.teacher_override,
        "effective_score": effective_score(row.score, row.teacher_override),
    }
