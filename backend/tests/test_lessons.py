from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assignment,
    AuditLog,
    IncidentType,
    InteractionEvent,
    Lesson,
    LessonParticipant,
    Scenario,
    StatusEvent,
    User,
)
from app.domain.cards import card_summary
from app.domain.lesson_settings import LessonSettings
from app.realtime.clock import tick
from tests.test_auth import sign_in


async def lesson_fixture(db: AsyncSession, count: int = 4) -> tuple[Lesson, list[Assignment]]:
    teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
    student = (await db.scalars(select(User).where(User.login == "student1"))).one()
    incident = (await db.scalars(select(IncidentType).where(IncidentType.code == "01.01.01"))).one()
    scenario = Scenario(
        title="Контрольная карточка",
        source="MANUAL",
        status="APPROVED",
        origin="OPERATOR_112",
        incident_type_id=incident.id,
        difficulty=3,
        card_payload={
            "registered_at": datetime.now(UTC).isoformat(),
            "operator_workstation": "ОП-034",
            "applicant": {"name": "Учебный заявитель", "phone": "+7 900 ***-**-11"},
            "address": {"raw": "Дубнинская улица, д. 28", "clarification": "Двор"},
            "attributes": list(incident.attributes.values()),
            "modifiers": [],
            "description": "Горит крыша частного дома. Люди вышли на улицу.",
            "notified_services": ["MCHS", "DDS_CHERTANOVO", "INFORMATION"],
        },
        reference={"expected_status": "ACCEPTED", "rationale": "СЕКРЕТ ЭТАЛОНА"},
    )
    lesson = Lesson(
        title="Контрольное занятие",
        teacher_id=teacher.id,
        status="RUNNING",
        started_at=datetime.now(UTC),
        settings=LessonSettings(card_interval_sec=5).model_dump(mode="json"),
    )
    db.add_all([lesson, scenario])
    await db.flush()
    db.add(LessonParticipant(lesson_id=lesson.id, student_id=student.id))
    rows = [
        Assignment(
            lesson_id=lesson.id,
            student_id=student.id,
            scenario_id=scenario.id,
            card_number=f"2026-0919-{number:06d}",
            created_at=datetime.now(UTC) + timedelta(microseconds=number),
        )
        for number in range(count)
    ]
    db.add_all(rows)
    await db.flush()
    return lesson, rows


async def test_delivery_limit_interval_and_released_slot(db: AsyncSession) -> None:
    lesson, rows = await lesson_fixture(db)
    now = datetime.now(UTC)
    assert len([item for item in await tick(db, now) if item[1] == "CARD_DELIVERED"]) == 1
    await tick(db, now + timedelta(seconds=4))
    assert rows[1].state == "QUEUED"
    await tick(db, now + timedelta(seconds=5))
    await tick(db, now + timedelta(seconds=10))
    await tick(db, now + timedelta(seconds=15))
    assert [row.state for row in rows] == ["DELIVERED"] * 3 + ["QUEUED"]
    rows[0].state, rows[0].closed_at = "CLOSED", now + timedelta(seconds=16)
    await db.flush()
    await tick(db, now + timedelta(seconds=16))
    assert rows[3].state == "DELIVERED"
    assert lesson.settings["max_concurrent_cards"] == 3
    assert (
        await db.scalar(
            select(func.count()).select_from(StatusEvent).where(StatusEvent.status == "ADDED")
        )
        == 4
    )


@pytest.mark.parametrize("opened", [False, True])
async def test_expiration_after_31_seconds_is_recorded_once(db: AsyncSession, opened: bool) -> None:
    _, rows = await lesson_fixture(db, 1)
    now = datetime.now(UTC)
    await tick(db, now)
    if opened:
        rows[0].opened_at, rows[0].state = now, "OPENED"
        await db.flush()
    await tick(db, now + timedelta(seconds=30))
    assert rows[0].state != "EXPIRED"
    await tick(db, now + timedelta(seconds=31))
    await tick(db, now + timedelta(seconds=32))
    assert rows[0].state == "EXPIRED"
    assert (
        await db.scalar(
            select(func.count())
            .select_from(InteractionEvent)
            .where(InteractionEvent.kind == "PRIMARY_EXPIRED")
        )
        == 1
    )


async def test_processing_deadline_preserves_card_and_primary_is_not_expired(
    db: AsyncSession,
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    lesson.settings = {**lesson.settings, "card_processing_deadline_sec": 40}
    now = datetime.now(UTC)
    await tick(db, now)
    rows[0].opened_at, rows[0].primary_status_at, rows[0].state = now, now, "PRIMARY_SET"
    await db.flush()
    await tick(db, now + timedelta(seconds=41))
    await tick(db, now + timedelta(seconds=42))
    assert rows[0].state == "PRIMARY_SET"
    assert list(await db.scalars(select(InteractionEvent.kind))) == ["PROCESSING_EXPIRED"]
    assert (await card_summary(db, rows[0], lesson, now + timedelta(seconds=42)))["is_overdue"]
    rows[0].closed_at = now + timedelta(seconds=20)
    rows[0].state = "CLOSED"
    await db.flush()
    assert not (await card_summary(db, rows[0], lesson, now + timedelta(days=1)))["is_overdue"]


async def test_first_open_is_atomic_idempotent_private_and_has_server_deadlines(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    await tick(db, datetime.now(UTC))
    await sign_in(client)
    url = f"/api/v1/student/assignments/{rows[0].id}"
    first = await client.get(url)
    assert first.status_code == 200, first.text
    assert "СЕКРЕТ" not in first.text and "reference" not in first.text
    assert "INFORMATION" not in first.text
    assert first.json()["state"] == "OPENED"
    assert first.json()["card"]["notification_list"][0]["is_own"]
    opened_at = rows[0].opened_at
    second = await client.get(url)
    assert rows[0].opened_at == opened_at
    assert (
        first.json()["timers"]["processing_deadline_at"]
        == second.json()["timers"]["processing_deadline_at"]
    )
    assert (
        await db.scalar(
            select(func.count()).select_from(StatusEvent).where(StatusEvent.status == "RECEIVED")
        )
        == 1
    )
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "CARD_OPENED")
        )
        == 1
    )
    payload = (await client.get("/api/v1/student/state")).json()
    assert len(payload["cards"]) == 1
    assert payload["lesson"]["id"] == str(lesson.id)
    assert payload["cards"][0]["current_status"] is None
    assert (await client.get(f"/api/v1/student/assignments/{uuid4()}")).status_code == 404
    rows[0].student_id = lesson.teacher_id
    await db.flush()
    assert (await client.get(url)).status_code == 404


async def test_expired_card_can_open_and_external_fields_are_removed(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, rows = await lesson_fixture(db, 1)
    scenario = await db.get(Scenario, rows[0].scenario_id)
    assert scenario
    scenario.origin = "EXTERNAL_SYSTEM"
    now = datetime.now(UTC) - timedelta(seconds=32)
    await tick(db, now)
    await tick(db, now + timedelta(seconds=31))
    await sign_in(client)
    payload = (await client.get(f"/api/v1/student/assignments/{rows[0].id}")).json()
    assert payload["state"] == "EXPIRED" and rows[0].opened_at is not None
    assert payload["card"]["attributes"] == [] and payload["card"]["modifiers"] == []
    assert payload["card"]["operator_workstation"] is None


async def test_lesson_lifecycle_validations_finish_and_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    fixture, rows = await lesson_fixture(db, 1)
    fixture.status = "PLANNED"
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    body: dict[str, Any] = {
        "title": "Новое занятие",
        "participants": [{"student_id": str(rows[0].student_id)}],
    }
    response = await client.post("/api/v1/teacher/lessons", json=body)
    assert response.status_code == 201, response.text
    lesson_id = response.json()["id"]
    prefix = f"/api/v1/teacher/lessons/{lesson_id}"
    assert (await client.post(prefix + "/start")).status_code == 400
    assert (
        await client.post(
            prefix + "/assign",
            json={
                "assignments": [
                    {"student_id": str(rows[0].student_id), "scenario_id": str(rows[0].scenario_id)}
                ]
            },
        )
    ).status_code == 200
    assert (await client.post(prefix + "/start")).json()["status"] == "RUNNING"
    assert (await client.post(prefix + "/start")).status_code == 409
    assert (await client.post(f"/api/v1/teacher/lessons/{fixture.id}/start")).status_code == 400
    await tick(db, datetime.now(UTC))
    assert (await client.post(prefix + "/finish")).json()["status"] == "FINISHED"
    assert (await client.post(prefix + "/finish")).status_code == 409
    assigned = (await db.scalars(select(Assignment).where(Assignment.lesson_id == lesson_id))).one()
    assert assigned.state == "CLOSED" and assigned.closed_at
    events = list(await db.scalars(select(AuditLog.action)))
    assert all(
        action in events
        for action in (
            "LESSON_CREATED",
            "LESSON_STARTED",
            "LESSON_FINISHED",
            "CARD_CLOSED_BY_TEACHER",
        )
    )
    assert (
        await client.post(
            "/api/v1/teacher/lessons", json={**body, "participants": body["participants"] * 2}
        )
    ).status_code == 400
    client.cookies.clear()
    await sign_in(client)
    assert (await client.get("/api/v1/student/state")).json()["lesson"] is None


@pytest.mark.parametrize(
    "settings",
    [
        {"primary_status_deadline_sec": 0},
        {"max_concurrent_cards": 0},
        {"card_interval_sec": -1},
        {"difficulty_range": [6, 1]},
        {"difficulty_range": [1, 11]},
        {"weights": {"timeliness": 0}},
        {"scenario_mode": "TICKET"},
        {"unknown": True},
    ],
)
def test_invalid_settings_are_rejected(settings: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LessonSettings.model_validate(settings)
