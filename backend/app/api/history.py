from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, TrainingUser
from app.api.errors import APIError
from app.api.learning import TeacherOnly
from app.db.models import (
    Assignment,
    AssignmentFeedback,
    GroupMember,
    LearningGroup,
    Lesson,
    LessonParticipant,
    Scenario,
    User,
)
from app.domain.pagination import next_cursor, page_offset

router = APIRouter(tags=["history"])


async def learning_student(db: AsyncSession, user: User, student_id: UUID | None) -> UUID:
    if user.role == "STUDENT":
        if student_id is not None and student_id != user.id:
            raise APIError(404, "NOT_FOUND", "История не найдена.")
        return user.id
    if student_id is None:
        raise APIError(400, "VALIDATION_ERROR", "Выберите обучающегося.")
    taught = await db.scalar(
        select(LessonParticipant.id)
        .join(Lesson)
        .where(Lesson.teacher_id == user.id, LessonParticipant.student_id == student_id)
        .limit(1)
    )
    grouped = await db.scalar(
        select(GroupMember.student_id)
        .join(LearningGroup)
        .where(LearningGroup.teacher_id == user.id, GroupMember.student_id == student_id)
        .limit(1)
    )
    if not taught and not grouped:
        raise APIError(404, "NOT_FOUND", "История не найдена.")
    return student_id


@router.get("/teacher/progress/students")
async def progress_students(db: DB, user: TeacherOnly) -> dict[str, Any]:
    taught = select(LessonParticipant.student_id).join(Lesson).where(Lesson.teacher_id == user.id)
    grouped = (
        select(GroupMember.student_id)
        .join(LearningGroup)
        .where(LearningGroup.teacher_id == user.id)
    )
    rows = await db.scalars(
        select(User)
        .where(or_(User.id.in_(taught), User.id.in_(grouped)))
        .order_by(User.last_name, User.first_name, User.id)
    )
    return {
        "items": [{"id": str(row.id), "name": f"{row.last_name} {row.first_name}"} for row in rows]
    }


@router.get("/learning/history")
async def history(
    db: DB,
    user: TrainingUser,
    student_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    identifier = await learning_student(db, user, student_id)
    query = (
        select(Assignment, Lesson, Scenario)
        .join(Lesson)
        .join(Scenario, Assignment.scenario_id == Scenario.id)
        .where(Assignment.student_id == identifier, Assignment.score.is_not(None))
    )
    if user.role == "TEACHER":
        query = query.where(Lesson.teacher_id == user.id)
    offset = page_offset(cursor)
    rows = list(
        (
            await db.execute(
                query.order_by(Assignment.closed_at.desc(), Assignment.id)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
    )
    shown = rows[:limit]
    feedback = list(
        await db.scalars(
            select(AssignmentFeedback)
            .where(AssignmentFeedback.assignment_id.in_([row[0].id for row in shown]))
            .order_by(AssignmentFeedback.created_at, AssignmentFeedback.id)
        )
    )
    return {
        "items": [
            {
                "assignment_id": str(assignment.id),
                "lesson_id": str(lesson.id),
                "lesson_title": lesson.title,
                "closed_at": assignment.closed_at,
                "card_number": assignment.card_number,
                "scenario_title": scenario.title,
                "difficulty": scenario.difficulty,
                "task_mode": assignment.task_mode,
                "score": assignment.score,
                "teacher_override": assignment.teacher_override,
                "feedback": [
                    {"id": str(note.id), "body": note.body, "created_at": note.created_at}
                    for note in feedback
                    if note.assignment_id == assignment.id
                ],
            }
            for assignment, lesson, scenario in shown
        ],
        "next_cursor": next_cursor(offset, limit, len(rows) > limit),
    }
