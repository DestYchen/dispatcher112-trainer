from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assignment,
    AuditLog,
    IncidentType,
    Lesson,
    Scenario,
    Service,
    User,
    Workstation,
)
from app.domain.security import password_hasher
from app.seeds.showcase import PASSWORD, prepare_showcase


async def test_showcase_prepares_two_real_exercises_without_starting_timers(
    db: AsyncSession,
) -> None:
    result = await prepare_showcase(db)
    assert await prepare_showcase(db) == result
    lessons = list(
        await db.scalars(
            select(Lesson).where(Lesson.id.in_([UUID(row["id"]) for row in result["lessons"]]))
        )
    )
    assert {row.settings["training_mode"] for row in lessons} == {"CARD_ENTRY", "CARD_ACTIONS"}
    assert all(row.status == "PLANNED" and row.started_at is None for row in lessons)
    assert all(
        row.settings["primary_status_deadline_sec"] == 30
        and row.settings["card_processing_deadline_sec"] == 300
        for row in lessons
    )
    cards = list(
        await db.scalars(
            select(Assignment).where(Assignment.lesson_id.in_([row.id for row in lessons]))
        )
    )
    assert len(cards) == 2 and all(
        row.state == "QUEUED" and row.delivered_at is None for row in cards
    )
    scenarios = list(
        await db.scalars(
            select(Scenario).where(Scenario.id.in_([row.scenario_id for row in cards]))
        )
    )
    assert all(
        row.status == "APPROVED" and row.reference["expected_status_chain"][-1] == "WORK_COMPLETED"
        for row in scenarios
    )
    assert sum(row.reference["report_required"] for row in scenarios) == 1
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "SHOWCASE_USER_CREATED")
        )
        == 2
    )
    for card in cards:
        assert await db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "ASSIGNMENT_CREATED", AuditLog.entity_id == card.id
            )
        )
    student = await db.scalar(select(User).where(User.login == result["student"]))
    assert student and password_hasher.verify(student.password_hash, PASSWORD)


async def test_showcase_preserves_running_lesson_password_and_audit(db: AsyncSession) -> None:
    result = await prepare_showcase(db)
    lesson = await db.get(Lesson, UUID(result["lessons"][0]["id"]))
    user = await db.scalar(select(User).where(User.login == result["teacher"]))
    assert lesson and user
    lesson.status, lesson.started_at = "RUNNING", datetime.now(UTC)
    started = lesson.started_at
    user.password_hash = password_hasher.hash("Changed-password-112")
    await db.flush()
    before = await db.scalar(select(func.count()).select_from(AuditLog))
    again = await prepare_showcase(db)
    assert again["lessons"][0]["id"] == str(lesson.id) and lesson.started_at == started
    assert password_hasher.verify(user.password_hash, "Changed-password-112")
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == before


async def test_showcase_keeps_finished_history_and_creates_next_exercise(db: AsyncSession) -> None:
    result = await prepare_showcase(db)
    original = await db.get(Lesson, UUID(result["lessons"][0]["id"]))
    assert original
    original.status = "FINISHED"
    original.finished_at = datetime.now(UTC)
    again = await prepare_showcase(db)
    assert again["lessons"][0]["id"] != str(original.id)
    assert again["lessons"][1]["id"] == result["lessons"][1]["id"]
    assert original.status == "FINISHED"
    assert await db.get(Lesson, original.id) is not None


@pytest.mark.parametrize("disabled", [False, True])
async def test_showcase_never_takes_over_an_existing_or_disabled_account(
    db: AsyncSession, disabled: bool
) -> None:
    if disabled:
        await prepare_showcase(db)
        user = await db.scalar(select(User).where(User.login == "demo.teacher"))
        assert user
        user.is_active = False
    else:
        user = User(
            login="demo.teacher",
            role="ADMIN",
            last_name="Existing",
            first_name="User",
            password_hash="unchanged",
        )
        db.add(user)
    await db.flush()
    before = user.password_hash
    with pytest.raises(ValueError, match="занята или отключена"):
        await prepare_showcase(db)
    assert user.password_hash == before


async def test_showcase_requires_classifier_without_replacing_it(db: AsyncSession) -> None:
    incident = await db.scalar(select(IncidentType).where(IncidentType.code == "01.01.01"))
    assert incident
    incident.code = "CUSTOM-REPLACEMENT"
    await db.flush()
    with pytest.raises(ValueError, match="сначала загрузите учебный классификатор"):
        await prepare_showcase(db)
    assert incident.code == "CUSTOM-REPLACEMENT"
    assert await db.scalar(select(User.id).where(User.login == "demo.teacher")) is None


@pytest.mark.parametrize("disabled", [False, True])
async def test_showcase_preserves_changed_workstation(db: AsyncSession, disabled: bool) -> None:
    await prepare_showcase(db)
    station = await db.scalar(select(Workstation).where(Workstation.number == "АРМ-ПОКАЗ"))
    assert station
    if disabled:
        station.is_active = False
    else:
        station.room = "Другой класс"
    await db.flush()
    with pytest.raises(ValueError, match="Рабочее место АРМ-ПОКАЗ занято или отключено"):
        await prepare_showcase(db)
    assert not station.is_active if disabled else station.room == "Другой класс"


async def test_showcase_preserves_changed_student_service(db: AsyncSession) -> None:
    await prepare_showcase(db)
    user = await db.scalar(select(User).where(User.login == "demo.student"))
    assert user
    other = await db.scalar(select(Service).where(Service.id != user.service_id))
    assert other
    user.service_id = other.id
    await db.flush()
    with pytest.raises(ValueError, match="изменена служба"):
        await prepare_showcase(db)
    assert user.service_id == other.id
