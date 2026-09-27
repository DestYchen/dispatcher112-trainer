import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, InteractionEvent, StatusEvent, User
from app.domain.teacher_live import live_snapshot
from app.realtime.clock import tick
from app.realtime.hub import Hub
from app.scoring.effective import effective_score
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_idle_after_ninety_seconds_includes_expired_and_ignores_automatic_events(
    db: AsyncSession,
) -> None:
    lesson, rows = await lesson_fixture(db, 2)
    now = datetime.now(UTC)
    await tick(db, now)
    at_boundary = await live_snapshot(db, lesson, now + timedelta(seconds=90))
    assert at_boundary["students"][0]["alert"] == "OVERDUE"
    await tick(db, now + timedelta(seconds=91))
    snapshot = await live_snapshot(db, lesson, now + timedelta(seconds=91))
    assert snapshot["students"][0]["alert"] == "IDLE"
    assert snapshot["students"][0]["active_cards"] == 2
    db.add(
        InteractionEvent(
            assignment_id=rows[0].id, kind="CARD_OPENED", created_at=now + timedelta(seconds=90)
        )
    )
    await db.flush()
    snapshot = await live_snapshot(db, lesson, now + timedelta(seconds=92))
    assert snapshot["students"][0]["alert"] == "OVERDUE"
    for row in rows:
        row.state, row.closed_at = "CLOSED", now + timedelta(seconds=93)
    await db.flush()
    snapshot = await live_snapshot(db, lesson, now + timedelta(seconds=1000))
    assert snapshot["students"][0]["alert"] is None
    assert snapshot["students"][0]["expired"] == 1


async def test_observation_is_read_only_and_private(client: AsyncClient, db: AsyncSession) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    await tick(db, datetime.now(UTC))
    await sign_in(client, "teacher", "teacher")
    before = await db.scalar(select(func.count()).select_from(StatusEvent))
    observed = await client.get(f"/api/v1/teacher/assignments/{rows[0].id}")
    assert observed.status_code == 200 and observed.json()["state"] == "DELIVERED"
    assert rows[0].opened_at is None and rows[0].state == "DELIVERED"
    assert await db.scalar(select(func.count()).select_from(StatusEvent)) == before
    assert (
        await client.get(
            f"/api/v1/teacher/lessons/{lesson.id}/students/{rows[0].student_id}/observe"
        )
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/teacher/lessons/{lesson.id}/students/{uuid4()}/observe")
    ).status_code == 404
    admin = (await db.scalars(select(User).where(User.login == "admin"))).one()
    lesson.teacher_id = admin.id
    await db.flush()
    assert (await client.get(f"/api/v1/teacher/assignments/{rows[0].id}")).status_code == 404
    assert (await client.get(f"/api/v1/teacher/lessons/{lesson.id}/live")).status_code == 404
    client.cookies.clear()
    await sign_in(client)
    assert (await client.get(f"/api/v1/teacher/lessons/{lesson.id}/live")).status_code == 403


async def test_finish_scores_unfinished_and_override_preserves_original_and_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, rows = await lesson_fixture(db, 2)
    lesson.settings = {**lesson.settings, "grammar_check_enabled": False}
    await tick(db, datetime.now(UTC))
    await sign_in(client, "teacher", "teacher")
    url = f"/api/v1/teacher/assignments/{rows[0].id}/override"
    assert (
        await client.post(url, json={"total": 70, "comment": "Ручная проверка"})
    ).status_code == 409
    assert (await client.post(f"/api/v1/teacher/lessons/{lesson.id}/finish")).status_code == 200
    original = deepcopy(rows[0].score)
    assert original is not None and rows[0].state == "CLOSED"
    assert rows[1].state == "QUEUED" and rows[1].score is None
    result = await client.post(
        url, json={"axes": {"correctness": 85}, "comment": "Решение обосновано."}
    )
    assert result.status_code == 200, result.text
    assert rows[0].score == original
    assert result.json()["effective_score"]["axes"]["correctness"]["score"] == 85
    correction = deepcopy(rows[0].teacher_override)
    result = await client.post(url, json={"total": 82, "comment": "Итог проверен повторно."})
    assert result.json()["effective_score"]["total"] == 82 and rows[0].score == original
    audits = list(
        await db.scalars(
            select(AuditLog).where(AuditLog.action == "SCORE_OVERRIDE").order_by(AuditLog.id)
        )
    )
    assert len(audits) == 2 and audits[1].payload and audits[1].payload["previous"] == correction
    snapshot = await live_snapshot(db, lesson, datetime.now(UTC))
    assert snapshot["students"][0]["current_score"] == 82
    assert snapshot["aggregate"]["cards_closed"] == 1
    assert snapshot["aggregate"]["top_violations"]
    client.cookies.clear()
    await sign_in(client)
    result = await client.get(f"/api/v1/student/results?lesson_id={lesson.id}")
    assert result.json()["summary"]["total"] == 82
    assert result.json()["cards"][0]["score"] == original
    assert (
        await client.post(url, json={"total": 100, "comment": "Не имеет прав"})
    ).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"total": -1, "comment": "Ошибка"},
        {"total": 101, "comment": "Ошибка"},
        {"axes": {"incorrect": 50}, "comment": "Ошибка"},
        {"axes": {"literacy": 101}, "comment": "Ошибка"},
        {"total": 80, "comment": "   "},
        {"comment": "Нет оценки"},
    ],
)
async def test_invalid_override_does_not_change_score(
    client: AsyncClient, db: AsyncSession, body: dict[str, Any]
) -> None:
    _, rows = await lesson_fixture(db, 1)
    await sign_in(client, "teacher", "teacher")
    result = await client.post(f"/api/v1/teacher/assignments/{rows[0].id}/override", json=body)
    assert result.status_code == 400 and rows[0].teacher_override is None


def test_axis_override_uses_effective_weights_without_mutation() -> None:
    original: dict[str, Any] = {
        "total": 60,
        "axes": {
            "correctness": {"score": 50, "weight": 0.8},
            "completeness": {"score": 100, "weight": 0.2},
            "literacy": {"score": None, "weight": 0},
        },
    }
    score = effective_score(original, {"axes": {"correctness": 100}})
    assert score["total"] == 100 and original["axes"]["correctness"]["score"] == 50


async def test_twenty_connections_and_two_hundred_updates_have_no_loss_or_reordering() -> None:
    class RecordingSocket:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def send_json(self, message: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            self.messages.append(message)

    hub = Hub()
    sockets = [RecordingSocket() for _ in range(20)]
    teacher = RecordingSocket()
    hub.join(cast(WebSocket, teacher), "teacher:lesson")
    for index, socket in enumerate(sockets):
        hub.join(cast(WebSocket, socket), f"user:{index}")
    for action in range(10):
        await asyncio.gather(
            *(
                hub.send(f"user:{index}", "CARD_DELIVERED", {"action": action})
                for index in range(20)
            )
        )
        await asyncio.gather(
            *(
                hub.send(
                    "teacher:lesson", "STUDENT_ACTION", {"student_id": str(index), "action": action}
                )
                for index in range(20)
            )
        )
    assert all(
        [message["payload"]["action"] for message in socket.messages] == list(range(10))
        for socket in sockets
    )
    assert len(teacher.messages) == 200
    for index in range(20):
        assert [
            message["payload"]["action"]
            for message in teacher.messages
            if message["payload"]["student_id"] == str(index)
        ] == list(range(10))
