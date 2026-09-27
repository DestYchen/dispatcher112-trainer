from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Assignment, Lesson, LessonParticipant, Scenario, User
from app.seeds.demo import prepare_demo


async def test_demo_is_idempotent_three_students_nine_approved_cards(db: AsyncSession) -> None:
    lesson_id = await prepare_demo(db)
    assert await prepare_demo(db) == lesson_id
    lesson = await db.get(Lesson, lesson_id)
    assert lesson and lesson.status == "PLANNED" and lesson.started_at is None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(LessonParticipant)
            .where(LessonParticipant.lesson_id == lesson_id)
        )
        == 3
    )
    cards = list(await db.scalars(select(Assignment).where(Assignment.lesson_id == lesson_id)))
    assert len(cards) == 9 and all(
        row.state == "QUEUED" and row.delivered_at is None for row in cards
    )
    assert len({row.student_id for row in cards}) == 3
    assert set(
        await db.scalars(select(User.login).where(User.id.in_([row.student_id for row in cards])))
    ) == {"student1", "student2", "student3"}
    scenarios = list(
        await db.scalars(
            select(Scenario).where(Scenario.id.in_([row.scenario_id for row in cards]))
        )
    )
    assert all(row.status == "APPROVED" for row in scenarios)
    assert {row.origin for row in scenarios} == {"OPERATOR_112", "EXTERNAL_SYSTEM"}
    assert any(row.reference["report_required"] for row in scenarios)
