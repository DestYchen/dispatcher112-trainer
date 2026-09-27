from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from starlette.websockets import WebSocket

from app.realtime.hub import Hub


async def test_room_isolation_duplicate_join_and_disconnect() -> None:
    captured: list[dict[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        captured.append(dict(message))

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    socket = WebSocket({"type": "websocket", "path": "/"}, receive, send)
    await socket.accept()
    captured.clear()
    hub = Hub()
    student_id = uuid4()
    hub.join(socket, f"user:{student_id}")
    hub.join(socket, f"user:{student_id}")
    hub.join(socket, "student:lesson")
    assert hub.online(student_id)
    await hub.send("user:someone-else", "CARD_DELIVERED", {"secret": True})
    await hub.send("teacher:lesson", "STUDENT_ACTION", {})
    assert captured == []
    await hub.send(
        f"user:{student_id}",
        "CARD_EXPIRED",
        {"assignment_id": "one"},
        datetime(2026, 9, 19, tzinfo=UTC),
    )
    assert len(captured) == 1
    assert '"ts":"2026-09-19T00:00:00+00:00"' in captured[0]["text"]
    hub.leave(socket)
    assert not hub.online(student_id) and not hub.rooms and not hub.locks


async def test_disconnected_receiver_does_not_block_the_room_or_later_events() -> None:
    captured: list[dict[str, Any]] = []

    async def closed_send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.send":
            raise OSError("Client disconnected during send")

    async def live_send(message: MutableMapping[str, Any]) -> None:
        captured.append(dict(message))

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    closed = WebSocket({"type": "websocket", "path": "/"}, receive, closed_send)
    live = WebSocket({"type": "websocket", "path": "/"}, receive, live_send)
    await closed.accept()
    await live.accept()
    captured.clear()
    hub = Hub()
    hub.join(closed, "user:one")
    hub.join(live, "user:one")
    await hub.send("user:one", "CARD_DELIVERED", {})
    await hub.send("user:one", "CARD_CLOSED", {})
    assert closed not in hub.locks and len(captured) == 2
