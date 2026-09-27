from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, InteractionEvent, StatusEvent
from app.realtime.clock import tick
from app.scoring.grammar import GrammarResult, check_grammar
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_preview_is_read_only_and_private_and_closure_scores_during_outage(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(text: str, field: str = "comment") -> GrammarResult:
        return GrammarResult(False, (), "Проверка грамотности недоступна.")

    monkeypatch.setattr("app.api.student.check_grammar", unavailable)
    monkeypatch.setattr("app.scoring.service.check_grammar", unavailable)
    lesson, rows = await lesson_fixture(db, 1)
    await tick(db, datetime.now(UTC))
    await sign_in(client)
    prefix = f"/api/v1/student/assignments/{rows[0].id}"
    await client.get(prefix)
    counts = [
        await db.scalar(select(func.count()).select_from(model))
        for model in (AuditLog, InteractionEvent, StatusEvent)
    ]
    response = await client.post(
        prefix + "/check-text", json={"text": "Дубининская улица", "field": "comment"}
    )
    assert response.status_code == 200 and response.json()["issues"][0]["kind"] == "ADDRESS_TYPO"
    assert response.json()["grammar_available"] is False
    assert counts == [
        await db.scalar(select(func.count()).select_from(model))
        for model in (AuditLog, InteractionEvent, StatusEvent)
    ]
    assert rows[0].score is None
    assert (
        await client.post(
            f"/api/v1/student/assignments/{uuid4()}/check-text",
            json={"text": "Улица", "field": "comment"},
        )
    ).status_code == 404
    for status in ("ACCEPTED", "WORK_COMPLETED"):
        result = await client.post(
            prefix + "/status",
            json={"status": status, "comment": "Работы на Дубининской улице выполнены."},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 200, result.text
    score = rows[0].score
    assert score and score["axes"]["literacy"]["skipped"]
    assert any(item["code"] == "ADDRESS_TYPO" for item in score["violations"])
    assert (await client.get(f"/api/v1/student/results?lesson_id={lesson.id}")).status_code == 404
    client.cookies.clear()
    await sign_in(client, "teacher", "teacher")
    assert (await client.post(f"/api/v1/teacher/lessons/{lesson.id}/finish")).status_code == 200
    assert rows[0].score == score
    client.cookies.clear()
    await sign_in(client)
    response = await client.get(f"/api/v1/student/results?lesson_id={lesson.id}")
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["cards_total"] == 1
    assert response.json()["cards"][0]["score"] == score
    assert (await client.get(f"/api/v1/student/results?lesson_id={uuid4()}")).status_code == 404


async def test_real_local_language_tool_finds_russian_spelling() -> None:
    result = await check_grammar("Машына приехала.")
    assert result.available
    assert result.items and all(item["kind"] == "SPELLING" for item in result.items)
    assert any(
        "машина" in [value.lower() for value in item["suggestions"]] for item in result.items
    )
