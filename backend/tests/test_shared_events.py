import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from starlette.websockets import WebSocket

from app.config import settings
from app.operations.worker_health import healthy
from app.realtime import relay
from app.realtime.clock import claim_clock_tick
from app.realtime.hub import Hub
from app.transport_security import redis_tls


@pytest.fixture
async def cache(client: AsyncClient) -> AsyncIterator[Redis]:
    connection: Redis = Redis.from_url(
        settings.redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True, **redis_tls()
    )
    yield connection
    await connection.aclose()


async def socket(events: list[dict[str, Any]], completed: asyncio.Event | None = None) -> WebSocket:
    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.send":
            events.append(json.loads(message["text"]))
            if completed is not None and events[-1]["type"] == "CARD_CLOSED":
                completed.set()

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    connection = WebSocket({"type": "websocket", "path": "/"}, receive, send)
    await connection.accept()
    return connection


async def test_cross_backend_delivery_survives_invalid_event_and_preserves_isolation(
    cache: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = Hub(), Hub()
    await first.start(cache)
    await second.start(cache)
    events: list[dict[str, Any]] = []
    completed = asyncio.Event()
    second.join(await socket(events, completed), "user:recipient")
    # A consumer cursor is per backend: both see the event, unlike a consumer group.
    await first.send("user:recipient", "CARD_DELIVERED", {"assignment_id": "one"})
    await cache.xadd("dispatcher:event-stream", {"event": "[]"})
    await first.send("user:other", "CARD_DELIVERED", {"private": True})
    await first.send("user:recipient", "CARD_CLOSED", {"assignment_id": "one"})
    monkeypatch.setattr(relay, "hub", second)
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: cache)
    task = asyncio.create_task(relay.stream_relay())
    try:
        async with asyncio.timeout(5):
            await completed.wait()
        assert [event["type"] for event in events] == ["CARD_DELIVERED", "CARD_CLOSED"]
        assert first.cursor == "0-0" and second.cursor != first.cursor
        independent = await cache.xread({"dispatcher:event-stream": first.cursor})
        assert len(independent[0][1]) == 4
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_presence_across_backends_keeps_other_connection_and_expires_crashed_node(
    cache: Redis,
) -> None:
    first, second = Hub(), Hub()
    await first.start(cache)
    await second.start(cache)
    one, two = await socket([]), await socket([])
    user, other = uuid4(), uuid4()
    await first.touch(one, user)
    await second.touch(two, user)
    assert await first.online_many([user, other]) == {user: True, other: False}
    await first.detach(one)
    assert await first.is_online(user)
    key, token = second.presence[two]
    await cache.zadd(key, {token: 1})
    assert not await first.is_online(user)


async def test_new_backend_starts_at_latest_event_and_does_not_replay_old_cards(
    cache: Redis,
) -> None:
    publisher = Hub()
    await publisher.start(cache)
    await publisher.send("user:recipient", "CARD_DELIVERED", {"assignment_id": "old"})
    subscriber = Hub()
    await subscriber.start(cache)
    assert subscriber.cursor != "0-0"
    assert not await cache.xread({"dispatcher:event-stream": subscriber.cursor})


async def test_scheduler_batch_preserves_order_rooms_and_server_time(cache: Redis) -> None:
    publisher = Hub()
    await publisher.start(cache)
    now = datetime.now(UTC)
    messages = [(f"user:{index}", "CARD_DELIVERED", {"index": index}) for index in range(100)]
    await publisher.send_many(messages, now)
    stored = await cache.xrange("dispatcher:event-stream")
    assert [json.loads(fields["event"]) for _, fields in stored] == [
        {"room": room, "type": kind, "ts": now.isoformat(), "payload": payload}
        for room, kind, payload in messages
    ]


async def test_one_scheduler_per_interval_and_expiry_allows_failover(cache: Redis) -> None:
    assert sum(await asyncio.gather(*(claim_clock_tick(cache) for _ in range(4)))) == 1
    assert 0 < await cache.pttl("dispatcher:clock-slot") <= 950
    await cache.pexpire("dispatcher:clock-slot", 1)
    await asyncio.sleep(0.01)
    assert await claim_clock_tick(cache)


async def test_scheduler_batch_without_redis_delivers_locally() -> None:
    publisher = Hub()
    events: list[dict[str, Any]] = []
    publisher.join(await socket(events), "user:one")
    now = datetime.now(UTC)
    await publisher.send_many(
        [
            ("user:one", "CARD_DELIVERED", {"index": 1}),
            ("user:other", "CARD_DELIVERED", {"index": 2}),
            ("user:one", "CARD_CLOSED", {"index": 1}),
        ],
        now,
    )
    assert [row["type"] for row in events] == ["CARD_DELIVERED", "CARD_CLOSED"]


async def test_worker_health_uses_the_live_expiring_queue_heartbeat(
    cache: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_URL", settings.redis_url.rsplit("/", 1)[0] + "/15")
    assert not healthy()
    await cache.set("arq:queue:health-check", "worker alive", ex=6)
    assert healthy()
    await cache.delete("arq:queue:health-check")
    assert not healthy()
