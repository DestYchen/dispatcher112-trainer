"""Create a separate classroom for the stage 8 acceptance script."""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

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


async def main() -> None:
    async with session_factory() as db, db.begin():
        demo_student = (await db.scalars(select(User).where(User.login == "student1"))).one()
        demo_teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
        people = []
        for index in range(21):
            login = "accept_teacher" if index == 0 else f"accept_student{index:02d}"
            user = await db.scalar(select(User).where(User.login == login))
            if user is None:
                user = User(
                    login=login,
                    password_hash=(demo_teacher if index == 0 else demo_student).password_hash,
                    role="TEACHER" if index == 0 else "STUDENT",
                    last_name="Проверка",
                    first_name=f"Участник {index:02d}",
                    service_id=demo_student.service_id,
                )
                db.add(user)
                await db.flush()
                db.add(
                    AuditLog(
                        action="ACCEPTANCE_USER_CREATED", entity_type="user", entity_id=user.id
                    )
                )
            people.append(user)
        teacher = people[0]
        lesson = Lesson(
            title="Приёмка пульта: 20 участников",
            teacher_id=teacher.id,
            status="PLANNED",
            settings=LessonSettings(
                primary_status_deadline_sec=120,
                card_processing_deadline_sec=600,
                card_interval_sec=1,
                grammar_check_enabled=False,
            ).model_dump(mode="json"),
        )
        db.add(lesson)
        await db.flush()
        scenario = (
            await db.scalars(select(Scenario).where(Scenario.title == "Учебный пожар: двор"))
        ).first()
        assert scenario
        participants = []
        for index, user in enumerate(people[1:], 1):
            number = f"АРМ-{index:02d}"
            station = await db.scalar(select(Workstation).where(Workstation.number == number))
            if station is None:
                station = Workstation(number=number)
                db.add(station)
                await db.flush()
            db.add(
                LessonParticipant(
                    lesson_id=lesson.id, student_id=user.id, workstation_id=station.id
                )
            )
            assignment = Assignment(
                lesson_id=lesson.id,
                student_id=user.id,
                scenario_id=scenario.id,
                card_number=f"ПУЛЬТ-{index:02d}",
            )
            db.add(assignment)
            await db.flush()
            participants.append(
                {"id": str(user.id), "login": user.login, "assignment_id": str(assignment.id)}
            )
        db.add(
            AuditLog(
                user_id=teacher.id,
                action="ACCEPTANCE_LESSON_CREATED",
                entity_type="lesson",
                entity_id=lesson.id,
            )
        )
        fixture = {"lesson_id": str(lesson.id), "teacher": teacher.login, "students": participants}
    await asyncio.to_thread(
        Path("/data/live-acceptance.json").write_text,
        json.dumps(fixture, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Created acceptance classroom with 20 participants")


if __name__ == "__main__":
    asyncio.run(main())
