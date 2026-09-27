"""Prepare two real exercises for a presentation, without starting or resetting lessons."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import (
    Assignment,
    AuditLog,
    IncidentType,
    Lesson,
    LessonParticipant,
    Scenario,
    Service,
    User,
    Workstation,
)
from app.domain.classifier import resolve_services
from app.domain.lesson_settings import LessonSettings
from app.domain.security import password_hasher

PASSWORD = "Showcase112!"
EXERCISES: tuple[tuple[str, Literal["CARD_ACTIONS", "CARD_ENTRY"]], ...] = (
    ("01 · Пожар в частном доме — действия ДДС", "CARD_ACTIONS"),
    ("02 · Приём обращения — заполнение карточки", "CARD_ENTRY"),
)


async def prepare_showcase(db: AsyncSession) -> dict[str, Any]:
    await db.execute(text("SELECT pg_advisory_xact_lock(112170)"))
    service = await db.scalar(select(Service).where(Service.code == "DDS_CHERTANOVO"))
    incident = await db.scalar(select(IncidentType).where(IncidentType.code == "01.01.01"))
    if service is None or incident is None:
        raise ValueError("Для показа сначала загрузите учебный классификатор из комплекта.")
    users = {}
    for role, surname, first, middle in (
        ("TEACHER", "Смирнова", "Анна", "Сергеевна"),
        ("STUDENT", "Иванов", "Павел", "Сергеевич"),
    ):
        login = "demo." + role.lower()
        user = await db.scalar(select(User).where(User.login == login))
        if user:
            owned = await db.scalar(
                select(AuditLog.id).where(
                    AuditLog.action == "SHOWCASE_USER_CREATED",
                    AuditLog.entity_id == user.id,
                )
            )
            if not owned or user.role != role or not user.is_active:
                raise ValueError(
                    f"Учётная запись {login} занята или отключена. Проверьте её у администратора."
                )
            if role == "STUDENT" and user.service_id != service.id:
                raise ValueError("У демонстрационного обучающегося изменена служба.")
        else:
            user = User(
                login=login,
                role=role,
                last_name=surname,
                first_name=first,
                middle_name=middle,
                service_id=service.id if role == "STUDENT" else None,
                password_hash=await asyncio.to_thread(password_hasher.hash, PASSWORD),
            )
            db.add(user)
            await db.flush()
            db.add(AuditLog(action="SHOWCASE_USER_CREATED", entity_type="users", entity_id=user.id))
        users[role] = user
    teacher, student = users["TEACHER"], users["STUDENT"]
    station = await db.scalar(select(Workstation).where(Workstation.number == "АРМ-ПОКАЗ"))
    if station is None:
        station = Workstation(number="АРМ-ПОКАЗ", room="Показ программы")
        db.add(station)
        await db.flush()
        db.add(
            AuditLog(action="WORKSTATION_CREATED", entity_type="workstations", entity_id=station.id)
        )
    elif not station.is_active or station.room != "Показ программы":
        raise ValueError("Рабочее место АРМ-ПОКАЗ занято или отключено.")
    notified = await resolve_services(db, incident.id, [])
    lessons = []
    for title, mode in EXERCISES:
        existing = await db.scalar(
            select(Lesson).where(
                Lesson.teacher_id == teacher.id,
                Lesson.title == title,
                Lesson.status.in_(["PLANNED", "RUNNING"]),
            )
        )
        if existing:
            lessons.append({"id": str(existing.id), "title": title, "status": existing.status})
            continue
        now = datetime.now(UTC)
        scenario = Scenario(
            title=title,
            source="MANUAL",
            status="APPROVED",
            origin="OPERATOR_112",
            incident_type_id=incident.id,
            difficulty=3,
            author_id=teacher.id,
            approved_by=teacher.id,
            approved_at=now,
            card_payload={
                "registered_at": now.isoformat(),
                "operator_workstation": "ОП-112",
                "applicant": {"name": "Учебный заявитель", "phone": "+7 900 ***-**-71"},
                "address": {"raw": "Дубнинская улица, д. 28", "clarification": "Вход со двора"},
                "attributes": list(incident.attributes.values()),
                "incident_type_name": incident.name,
                "modifiers": [],
                "description": "Пожар в частном доме. Люди вышли на улицу, пострадавших нет.",
                "notified_services": [row.code for row in notified],
            },
            reference={
                "expected_status": "ACCEPTED",
                "expected_status_chain": [
                    "ACCEPTED",
                    "RESPONSE_STARTED",
                    "ARRIVED",
                    "WORK_IN_PROGRESS",
                    "WORK_COMPLETED",
                ],
                "comment_required": False,
                "comment_must_contain": [],
                "report_required": mode == "CARD_ACTIONS",
                "report_callee_code": "DUTY_OFFICER" if mode == "CARD_ACTIONS" else None,
                "report_must_mention": ["address", "incident_type", "victims"]
                if mode == "CARD_ACTIONS"
                else [],
                "entry_description_keywords": ["пожар", "пострадавших нет"],
                "rationale": "Пожар на территории района требует реагирования ДДС.",
                "trap": None,
            },
        )
        lesson = Lesson(
            title=title,
            teacher_id=teacher.id,
            status="PLANNED",
            settings=LessonSettings(
                training_mode=mode,
                card_processing_deadline_sec=300,
                max_concurrent_cards=1,
                hints_enabled=True,
            ).model_dump(mode="json"),
        )
        db.add_all([scenario, lesson])
        await db.flush()
        assignment = Assignment(
            lesson_id=lesson.id,
            student_id=student.id,
            scenario_id=scenario.id,
            card_number=f"ПОКАЗ-{lesson.id.hex[:8]}",
            task_mode=mode,
        )
        db.add_all(
            [
                assignment,
                LessonParticipant(
                    lesson_id=lesson.id,
                    student_id=student.id,
                    workstation_id=station.id,
                ),
            ]
        )
        await db.flush()
        for action, entity, identity in (
            ("MANUAL_FIXTURE_IMPORTED", "scenario", scenario.id),
            ("LESSON_CREATED", "lessons", lesson.id),
            ("ASSIGNMENT_CREATED", "assignment", assignment.id),
        ):
            db.add(
                AuditLog(
                    user_id=teacher.id,
                    action=action,
                    entity_type=entity,
                    entity_id=identity,
                    payload={"source": "showcase"},
                )
            )
        lessons.append({"id": str(lesson.id), "title": title, "status": "PLANNED"})
    return {"teacher": teacher.login, "student": student.login, "lessons": lessons}


async def main() -> None:
    async with session_factory() as db, db.begin():
        result = await prepare_showcase(db)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
