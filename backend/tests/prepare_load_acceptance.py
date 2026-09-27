"""An isolated classroom with configurable real users and assignments."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from time import time

from sqlalchemy import select

from app.db.base import session_factory
from app.db.models import Assignment, AuditLog, Lesson, LessonParticipant, Scenario, User
from app.domain.lesson_settings import LessonSettings


async def main(users: int = 20, cards: int = 10) -> None:
    if not 1 <= users <= 100 or not 1 <= cards <= 10:
        raise ValueError("Acceptance supports 1–100 users and 1–10 cards each")
    suffix = str(int(time()))
    async with session_factory() as db, db.begin():
        student = (await db.scalars(select(User).where(User.login == "student1"))).one()
        teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
        people = []
        for index in range(users + 1):
            person = User(
                login=f"load{suffix}_{index:02d}",
                password_hash=(teacher if index == 0 else student).password_hash,
                role="TEACHER" if index == 0 else "STUDENT",
                last_name="Нагрузка",
                first_name=f"Участник {index:02d}",
                service_id=student.service_id,
            )
            db.add(person)
            await db.flush()
            db.add(AuditLog(action="LOAD_USER_CREATED", entity_type="users", entity_id=person.id))
            people.append(person)
        lesson = Lesson(
            title=f"Нагрузочная приёмка {users} × {cards}: {suffix}",
            teacher_id=people[0].id,
            status="PLANNED",
            settings=LessonSettings(
                primary_status_deadline_sec=120,
                card_processing_deadline_sec=600,
                card_interval_sec=1,
                grammar_check_enabled=True,
            ).model_dump(mode="json"),
        )
        db.add(lesson)
        await db.flush()
        scenario = (
            await db.scalars(select(Scenario).where(Scenario.title == "Учебный пожар: двор"))
        ).one()
        participants = []
        for index, person in enumerate(people[1:], 1):
            db.add(LessonParticipant(lesson_id=lesson.id, student_id=person.id))
            assignments = []
            for number in range(cards):
                assignment = Assignment(
                    lesson_id=lesson.id,
                    student_id=person.id,
                    scenario_id=scenario.id,
                    card_number=f"Н-{suffix}-{index:02d}-{number:02d}",
                    created_at=datetime.now(UTC) + timedelta(microseconds=number),
                )
                db.add(assignment)
                await db.flush()
                assignments.append(str(assignment.id))
            participants.append(
                {"id": str(person.id), "login": person.login, "assignments": assignments}
            )
        db.add(
            AuditLog(
                user_id=people[0].id,
                action="LOAD_LESSON_CREATED",
                entity_type="lessons",
                entity_id=lesson.id,
            )
        )
        fixture = {
            "lesson_id": str(lesson.id),
            "teacher": people[0].login,
            "students": participants,
        }
    print(json.dumps(fixture))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--cards", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(main(args.users, args.cards))
