"""Audited synthetic trajectories for UI/ML acceptance; never real learner evidence."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from time import time

from sqlalchemy import select

from app.db.base import session_factory
from app.db.models import Assignment, AuditLog, Lesson, LessonParticipant, Scenario, User
from app.domain.lesson_settings import LessonSettings
from tests.test_learning_model import synthetic_records


async def main() -> None:
    suffix = str(int(time()))
    async with session_factory() as db, db.begin():
        teacher_source = (await db.scalars(select(User).where(User.login == "teacher"))).one()
        student_source = (await db.scalars(select(User).where(User.login == "student1"))).one()
        teacher = User(
            login=f"learning{suffix}_teacher",
            password_hash=teacher_source.password_hash,
            first_name="Проверка",
            last_name="Учебный блок",
            role="TEACHER",
        )
        db.add(teacher)
        await db.flush()
        users = []
        for index in range(20):
            user = User(
                login=f"learning{suffix}_{index:02d}",
                password_hash=student_source.password_hash,
                first_name=f"Участник {index:02d}",
                last_name="Учебный блок",
                role="STUDENT",
                service_id=student_source.service_id,
            )
            db.add(user)
            users.append(user)
        await db.flush()
        template = (
            await db.scalars(select(Scenario).where(Scenario.title == "Учебный пожар: двор"))
        ).one()
        scenarios = {}
        for difficulty in range(1, 11):
            scenario = Scenario(
                title=f"Синтетический стенд: сложность {difficulty}",
                source="MANUAL",
                status="APPROVED",
                origin=template.origin,
                incident_type_id=template.incident_type_id,
                difficulty=difficulty,
                card_payload={**template.card_payload, "acceptance_fixture": "SYNTHETIC"},
                reference=template.reference,
                author_id=teacher.id,
                approved_by=teacher.id,
            )
            db.add(scenario)
            await db.flush()
            scenarios[difficulty] = scenario.id
        lesson = Lesson(
            title=f"Синтетические траектории для проверки ИИ {suffix}",
            teacher_id=teacher.id,
            status="FINISHED",
            settings=LessonSettings().model_dump(mode="json"),
            started_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            finished_at=datetime.fromisoformat("2026-01-02T00:00:00+00:00"),
        )
        db.add(lesson)
        await db.flush()
        for user in users:
            db.add(LessonParticipant(lesson_id=lesson.id, student_id=user.id))
            db.add(
                AuditLog(
                    user_id=teacher.id,
                    action="LEARNING_ACCEPTANCE_USER",
                    entity_type="user",
                    entity_id=user.id,
                    payload={"synthetic": True},
                )
            )
        assignments = []
        weights = {"timeliness": 0.3, "correctness": 0.4, "completeness": 0.2, "literacy": 0.1}
        for record in synthetic_records():
            at = datetime.fromisoformat(record["closed_at"])
            axes = {
                axis: {"score": score, "weight": weights[axis]}
                for axis, score in {**record["axes"], "literacy": 100}.items()
            }
            row = Assignment(
                lesson_id=lesson.id,
                student_id=users[int(record["student_id"])].id,
                scenario_id=scenarios[record["difficulty"]],
                card_number="ML-" + record["id"],
                task_mode=record["task_mode"],
                state="CLOSED",
                delivered_at=at - timedelta(seconds=30),
                opened_at=at - timedelta(seconds=25),
                primary_status_at=at - timedelta(seconds=25),
                closed_at=at,
                score={
                    "total": round(
                        sum(value["score"] * value["weight"] for value in axes.values()), 2
                    ),
                    "axes": axes,
                    "violations": [],
                    "computed_at": at.isoformat(),
                    "engine_version": "SYNTHETIC-ACCEPTANCE",
                    "timings": {
                        "primary_delay_ms": 5000,
                        "primary_deadline_ms": 30000,
                        "processing_ms": 25000,
                        "processing_deadline_ms": 180000,
                    },
                    "grammar": {
                        "critical": 0,
                        "minor": 0,
                        "items": [],
                        "available": True,
                        "skip_reason": None,
                    },
                },
            )
            db.add(row)
            assignments.append(row)
        await db.flush()
        db.add(
            AuditLog(
                user_id=teacher.id,
                action="LEARNING_ACCEPTANCE_CREATED",
                entity_type="lesson",
                entity_id=lesson.id,
                payload={
                    "synthetic": True,
                    "generated_axis_scores": True,
                    "students": 20,
                    "assignments": len(assignments),
                },
            )
        )
        result = {
            "teacher": teacher.login,
            "lesson_id": str(lesson.id),
            "participants": [
                {
                    "login": user.login,
                    "id": str(user.id),
                    "name": f"{user.last_name} {user.first_name}",
                }
                for user in users
            ],
            "assignment_id": str(assignments[0].id),
            "scenario_id": str(scenarios[3]),
        }
    await asyncio.to_thread(
        Path("/data/learning-acceptance.json").write_text,
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Prepared 20 synthetic learners / 300 scores, explicitly marked in source and audit")


if __name__ == "__main__":
    asyncio.run(main())
