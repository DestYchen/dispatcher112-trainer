"""Create an isolated voice classroom; all test data is retained with audit."""

import asyncio
import json
from pathlib import Path
from time import time

from sqlalchemy import select

from app.db.base import session_factory
from app.db.models import Assignment, AuditLog, Lesson, LessonParticipant, Scenario, User
from app.domain.lesson_settings import LessonSettings


async def main() -> None:
    suffix = str(int(time()))
    async with session_factory() as db, db.begin():
        source = (await db.scalars(select(User).where(User.login == "student1"))).one()
        teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
        people = []
        for index in range(21):
            user = User(
                login=f"sip{suffix}_{index:02d}",
                password_hash=(teacher if index == 0 else source).password_hash,
                role="TEACHER" if index == 0 else "STUDENT",
                service_id=source.service_id,
                last_name="Телефония",
                first_name=f"Участник {index:02d}",
            )
            db.add(user)
            await db.flush()
            db.add(
                AuditLog(
                    action="SIP_ACCEPTANCE_USER_CREATED", entity_type="users", entity_id=user.id
                )
            )
            people.append(user)
        lesson = Lesson(
            title=f"Приёмка SIP: {suffix}",
            teacher_id=people[0].id,
            status="PLANNED",
            settings=LessonSettings(
                training_mode="CARD_ENTRY",
                incoming_channel="VOICE",
                primary_status_deadline_sec=3600,
                card_processing_deadline_sec=3600,
                grammar_check_enabled=False,
            ).model_dump(mode="json"),
        )
        db.add(lesson)
        await db.flush()
        scenario = (
            await db.scalars(select(Scenario).where(Scenario.title == "Учебный пожар: двор"))
        ).one()
        participants = []
        for index, user in enumerate(people[1:], 1):
            db.add(LessonParticipant(lesson_id=lesson.id, student_id=user.id))
            assignment = Assignment(
                lesson_id=lesson.id,
                student_id=user.id,
                scenario_id=scenario.id,
                card_number=f"SIP-{suffix}-{index:02d}",
                task_mode="CARD_ENTRY",
            )
            db.add(assignment)
            await db.flush()
            participants.append(
                {"login": user.login, "id": str(user.id), "assignment_id": str(assignment.id)}
            )
        db.add(
            AuditLog(
                user_id=people[0].id,
                action="SIP_ACCEPTANCE_CREATED",
                entity_type="lesson",
                entity_id=lesson.id,
            )
        )
        result = {
            "teacher": people[0].login,
            "lesson_id": str(lesson.id),
            "participants": participants,
            "reference": {
                **scenario.card_payload,
                "incident_type_id": str(scenario.incident_type_id),
            },
        }
    await asyncio.to_thread(
        Path("/data/sip-acceptance.json").write_text,
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Created isolated voice classroom with 20 participants")


if __name__ == "__main__":
    asyncio.run(main())
