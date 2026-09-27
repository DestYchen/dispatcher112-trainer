"""Prepare a repeatable three-student demonstration without starting its timers."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import (
    Assignment,
    AuditLog,
    Lesson,
    LessonParticipant,
    Scenario,
    User,
    Workstation,
)
from app.domain.lesson_settings import LessonSettings
from app.seeds.manual_scenarios import manual_scenarios

TITLE = "Демонстрация АРМ-112: три рабочих места"


async def prepare_demo(db: AsyncSession) -> UUID:
    teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
    existing = await db.scalar(
        select(Lesson.id).where(
            Lesson.title == TITLE,
            Lesson.teacher_id == teacher.id,
            Lesson.status.in_(["PLANNED", "RUNNING"]),
        )
    )
    if existing:
        return existing
    await manual_scenarios(db)
    first = (await db.scalars(select(User).where(User.login == "student1"))).one()
    students = [first]
    for login, surname in (("student2", "Петрова"), ("student3", "Сидоров")):
        user = await db.scalar(select(User).where(User.login == login))
        if user is None:
            user = User(
                login=login,
                password_hash=first.password_hash,
                role="STUDENT",
                last_name=surname,
                first_name="Анна" if login == "student2" else "Пётр",
                service_id=first.service_id,
            )
            db.add(user)
            await db.flush()
            db.add(AuditLog(action="DEMO_USER_CREATED", entity_type="users", entity_id=user.id))
        students.append(user)
    lesson = Lesson(
        title=TITLE,
        teacher_id=teacher.id,
        status="PLANNED",
        settings=LessonSettings(card_interval_sec=15).model_dump(mode="json"),
    )
    db.add(lesson)
    await db.flush()
    titles = [
        "Учебный пожар: двор",
        "Учебный пожар: внешняя система",
        "Учебный пожар: доклад дежурному",
    ]
    scenarios = {
        row.title: row
        for row in await db.scalars(
            select(Scenario).where(Scenario.title.in_(titles), Scenario.source == "MANUAL")
        )
    }
    now = datetime.now(UTC)
    for index, user in enumerate(students, 7):
        station = await db.scalar(
            select(Workstation).where(Workstation.number == f"АРМ-{index:02d}")
        )
        if station is None:
            station = Workstation(number=f"АРМ-{index:02d}", room="Учебный класс")
            db.add(station)
            await db.flush()
            db.add(
                AuditLog(
                    action="WORKSTATION_CREATED", entity_type="workstations", entity_id=station.id
                )
            )
        db.add(
            LessonParticipant(lesson_id=lesson.id, student_id=user.id, workstation_id=station.id)
        )
        for number, title in enumerate(titles, 1):
            assignment = Assignment(
                lesson_id=lesson.id,
                student_id=user.id,
                scenario_id=scenarios[title].id,
                card_number=f"ДЕМО-{lesson.id.hex[:6]}-{index}-{number}",
                created_at=now + timedelta(microseconds=number),
            )
            db.add(assignment)
            await db.flush()
            db.add(
                AuditLog(
                    user_id=teacher.id,
                    action="ASSIGNMENT_CREATED",
                    entity_type="assignment",
                    entity_id=assignment.id,
                    payload={"source": "demo", "lesson_id": str(lesson.id)},
                )
            )
    db.add(
        AuditLog(
            user_id=teacher.id,
            action="LESSON_CREATED",
            entity_type="lessons",
            entity_id=lesson.id,
            payload={"source": "demo", "participants": [str(user.id) for user in students]},
        )
    )
    return lesson.id


async def main() -> None:
    async with session_factory() as db, db.begin():
        lesson_id = await prepare_demo(db)
    print(
        f"Готово занятие {lesson_id}: teacher/teacher; student1, student2, student3 / student. "
        "Нажмите «Начать занятие»."
    )


if __name__ == "__main__":
    asyncio.run(main())
