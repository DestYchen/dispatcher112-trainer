from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, RequestReceipt, StatusEvent
from app.realtime.clock import tick
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_complete_status_path_and_idempotent_terminal_retry(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, rows = await lesson_fixture(db, 1)
    await tick(db, datetime.now(UTC))
    await sign_in(client)
    prefix = f"/api/v1/student/assignments/{rows[0].id}"
    await client.get(prefix)
    for status in ["ACCEPTED", "RESPONSE_STARTED", "ARRIVED", "WORK_IN_PROGRESS", "WORK_COMPLETED"]:
        key = str(uuid4())
        response = await client.post(
            prefix + "/status", json={"status": status}, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 200, response.text
        retry = await client.post(
            prefix + "/status", json={"status": status}, headers={"Idempotency-Key": key}
        )
        assert response.json() == retry.json()
    assert rows[0].state == "CLOSED" and rows[0].closed_at and rows[0].primary_status_at
    assert response.json()["my_block"]["available_statuses"] == []
    assert (
        await db.scalar(
            select(func.count()).select_from(StatusEvent).where(StatusEvent.is_automatic.is_(False))
        )
        == 5
    )
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "STATUS_CHANGED")
        )
        == 5
    )
    closed = await client.post(
        prefix + "/status", json={"status": "ARRIVED"}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "CARD_CLOSED"


async def test_invalid_and_system_transitions_comment_rules_and_primary_not_reset(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, rows = await lesson_fixture(db, 1)
    await tick(db, datetime.now(UTC) - timedelta(seconds=31))
    await tick(db, datetime.now(UTC))
    await sign_in(client)
    prefix = f"/api/v1/student/assignments/{rows[0].id}"
    headers = {"Idempotency-Key": str(uuid4())}
    assert (
        await client.post(prefix + "/status", json={"status": "ACCEPTED"}, headers=headers)
    ).status_code == 409
    await client.get(prefix)
    for status in ["ADDED", "RECEIVED", "ARRIVED", "WORK_COMPLETED"]:
        result = await client.post(prefix + "/status", json={"status": status}, headers=headers)
        assert result.status_code == 409 and result.json()["error"]["code"] == "INVALID_TRANSITION"
    for comment in [None, "", " ", "а" * 14, " " + "а" * 14 + " "]:
        result = await client.post(
            prefix + "/status", json={"status": "NOT_ACCEPTED", "comment": comment}, headers=headers
        )
        assert result.status_code == 422 and result.json()["error"]["code"] == "COMMENT_REQUIRED"
    assert (
        await client.post(
            prefix + "/status",
            json={
                "status": "NOT_ACCEPTED",
                "comment": "Передано в МЧС: пожар на территории объекта.",
            },
            headers=headers,
        )
    ).status_code == 200
    first_time = rows[0].primary_status_at
    assert first_time is not None
    state = (await client.get("/api/v1/student/state")).json()
    assert state["cards"][0]["is_overdue"]
    assert (
        await client.post(
            prefix + "/status",
            json={"status": "ACCEPTED"},
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).status_code == 200
    assert rows[0].primary_status_at == first_time
    refused = await client.post(
        prefix + "/status",
        json={"status": "WORK_REFUSED"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert refused.status_code == 422


async def test_idempotency_scope_payload_mismatch_and_expiry(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, rows = await lesson_fixture(db, 2)
    now = datetime.now(UTC)
    await tick(db, now - timedelta(seconds=6))
    await tick(db, now)
    await sign_in(client)
    headers = {"Idempotency-Key": "same-key"}
    for row in rows:
        url = f"/api/v1/student/assignments/{row.id}"
        await client.get(url)
        assert (
            await client.post(url + "/status", json={"status": "ACCEPTED"}, headers=headers)
        ).status_code == 200
    url = f"/api/v1/student/assignments/{rows[0].id}/status"
    assert (await client.post(url, json={"status": "ARRIVED"}, headers=headers)).status_code == 400
    receipt = (
        await db.scalars(select(RequestReceipt).where(RequestReceipt.assignment_id == rows[0].id))
    ).one()
    receipt.expires_at = now - timedelta(seconds=1)
    await db.flush()
    assert (await client.post(url, json={"status": "ARRIVED"}, headers=headers)).status_code == 200
    assert (await client.post(url, json={"status": "WORK_COMPLETED"})).status_code == 400
    assert (
        await client.post(
            f"/api/v1/student/assignments/{uuid4()}/status",
            json={"status": "ACCEPTED"},
            headers=headers,
        )
    ).status_code == 404
