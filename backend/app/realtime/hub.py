import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import RedisError


class Hub:
    def __init__(self) -> None:
        self.rooms: dict[str, set[WebSocket]] = {}
        self.locks: dict[WebSocket, asyncio.Lock] = {}
        self.cache: Redis | None = None
        self.cursor = "0-0"
        self.presence: dict[WebSocket, tuple[str, str]] = {}

    async def start(self, cache: Redis) -> None:
        self.cache = cache
        latest = await cache.xrevrange("dispatcher:event-stream", count=1)
        self.cursor = latest[0][0] if latest else "0-0"

    async def online_many(self, users: list[UUID]) -> dict[UUID, bool]:
        if self.cache is None:
            return {user: self.online(user) for user in users}
        async with self.cache.pipeline(transaction=False) as pipe:
            for user in users:
                key = f"dispatcher:presence:{user}"
                pipe.zremrangebyscore(key, "-inf", datetime.now(UTC).timestamp())
                pipe.zcard(key)
            values = await pipe.execute()
        return {user: bool(values[index * 2 + 1]) for index, user in enumerate(users)}

    async def is_online(self, user: UUID) -> bool:
        return (await self.online_many([user]))[user]

    async def touch(self, socket: WebSocket, user: UUID) -> None:
        if self.cache is None:
            return
        key, token = self.presence.setdefault(socket, (f"dispatcher:presence:{user}", str(uuid4())))
        async with self.cache.pipeline() as pipe:
            pipe.zadd(key, {token: datetime.now(UTC).timestamp() + 15})
            pipe.expire(key, 30)
            await pipe.execute()

    async def detach(self, socket: WebSocket) -> None:
        record = self.presence.pop(socket, None)
        self.leave(socket)
        if self.cache is not None and record:
            try:
                await self.cache.zrem(*record)
            except RedisError:
                logging.getLogger(__name__).warning(
                    "Соединение исчезнет из присутствия по тайм-ауту"
                )

    def join(self, socket: WebSocket, room: str) -> None:
        self.rooms.setdefault(room, set()).add(socket)
        self.locks.setdefault(socket, asyncio.Lock())

    def leave(self, socket: WebSocket) -> None:
        for room in list(self.rooms):
            self.rooms[room].discard(socket)
            if not self.rooms[room]:
                del self.rooms[room]
        self.locks.pop(socket, None)

    def online(self, user_id: UUID) -> bool:
        return bool(self.rooms.get(f"user:{user_id}"))

    async def send(
        self, room: str, kind: str, payload: dict[str, Any], now: datetime | None = None
    ) -> None:
        message = {"type": kind, "ts": (now or datetime.now(UTC)).isoformat(), "payload": payload}
        if self.cache is not None:
            try:
                await self.cache.xadd(
                    "dispatcher:event-stream",
                    {"event": json.dumps({"room": room, **message}, ensure_ascii=False)},
                    maxlen=10000,
                    approximate=True,
                )
                return
            except RedisError:
                logging.getLogger(__name__).warning(
                    "Общая доставка недоступна; состояние сохранено в БД"
                )
        await self.deliver(room, message)

    async def deliver(self, room: str, message: dict[str, Any]) -> None:

        async def deliver(socket: WebSocket) -> None:
            lock = self.locks.get(socket)
            if lock is None:
                return
            try:
                async with lock, asyncio.timeout(1):
                    await socket.send_json(message)
            except (TimeoutError, RuntimeError, OSError, WebSocketDisconnect):
                self.leave(socket)

        sockets = tuple(self.rooms.get(room, ()))
        if len(sockets) == 1:
            await deliver(sockets[0])
        elif sockets:
            await asyncio.gather(*(deliver(socket) for socket in sockets))

    async def send_many(
        self, messages: list[tuple[str, str, dict[str, Any]]], now: datetime
    ) -> None:
        if not messages:
            return
        if self.cache is None:
            for room, kind, payload in messages:
                await self.send(room, kind, payload, now)
            return
        # Publish one committed scheduler batch without a network round trip per card.
        # Event order is retained; every backend still consumes its own stream cursor.
        async with self.cache.pipeline(transaction=False) as pipe:
            for room, kind, payload in messages:
                pipe.xadd(
                    "dispatcher:event-stream",
                    {
                        "event": json.dumps(
                            {"room": room, "type": kind, "ts": now.isoformat(), "payload": payload},
                            ensure_ascii=False,
                        )
                    },
                    maxlen=10000,
                    approximate=True,
                )
            await pipe.execute()


hub = Hub()
