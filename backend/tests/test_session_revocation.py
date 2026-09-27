from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket

from app.api.deps import decode_session
from app.config import settings
from app.db.models import User
from app.realtime.socket import websocket
from app.transport_security import redis_tls
from tests.test_auth import sign_in


async def check_socket(
    client: AsyncClient, db: AsyncSession, *, revoked: bool
) -> list[dict[str, Any]]:
    await sign_in(client)
    token = client.cookies.get("session") or ""
    claims = decode_session(token)
    cache: Redis = Redis.from_url(
        settings.redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True, **redis_tls()
    )
    captured: list[dict[str, Any]] = []
    connected = False

    async def send(message: MutableMapping[str, Any]) -> None:
        captured.append(dict(message))

    async def receive() -> dict[str, Any]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        row = await db.get(User, UUID(claims["sub"]))
        assert row
        row.role = "TEACHER"
        await db.flush()
        return {"type": "websocket.receive", "text": "HEARTBEAT"}

    if revoked:
        await cache.set(f"user_sessions_before:{claims['sub']}", str(datetime.now(UTC).timestamp()))
    socket = WebSocket(
        {
            "type": "websocket",
            "path": "/api/v1/ws",
            "headers": [
                (b"cookie", f"session={token}".encode()),
                (b"origin", settings.cors_origins[0].encode()),
            ],
            "query_string": b"role=student",
        },
        receive,
        send,
    )
    try:
        await websocket(socket, db, cache, "student")
        return captured
    finally:
        await cache.aclose()


async def test_password_reset_revocation_rejects_websocket(
    client: AsyncClient, db: AsyncSession
) -> None:
    events = await check_socket(client, db, revoked=True)
    assert events == [{"type": "websocket.close", "code": 1008, "reason": ""}]


async def test_role_change_closes_previously_authorized_websocket(
    client: AsyncClient, db: AsyncSession
) -> None:
    events = await check_socket(client, db, revoked=False)
    assert events[0]["type"] == "websocket.accept"
    assert events[-1]["type"] == "websocket.close" and events[-1]["code"] == 1008
