import asyncio
import json
import logging

from redis.asyncio import Redis

from app.config import settings
from app.realtime.hub import hub
from app.transport_security import redis_tls


async def legacy_relay() -> None:
    while True:
        client: Redis = Redis.from_url(settings.redis_url, decode_responses=True, **redis_tls())
        try:
            async with client.pubsub() as channel:
                await channel.subscribe("dispatcher:events")
                async for message in channel.listen():
                    if message["type"] != "message":
                        continue
                    event = json.loads(message["data"])
                    from datetime import UTC, datetime

                    await hub.deliver(
                        event["room"],
                        {
                            "type": event["type"],
                            "payload": event["payload"],
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )
        except Exception:
            logging.getLogger(__name__).exception("Связь с очередью событий прервана")
            await asyncio.sleep(1)
        finally:
            await client.aclose()


async def stream_relay() -> None:
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True, **redis_tls())
    try:
        while True:
            try:
                batches = await client.xread(
                    {"dispatcher:event-stream": hub.cursor}, count=100, block=1000
                )
                for _, entries in batches:
                    for identity, fields in entries:
                        try:
                            event = json.loads(fields["event"])
                            if (
                                not isinstance(event, dict)
                                or not all(
                                    isinstance(event.get(key), str)
                                    for key in ("room", "type", "ts")
                                )
                                or not isinstance(event.get("payload"), dict)
                            ):
                                raise ValueError("Invalid shared event")
                        except (KeyError, TypeError, ValueError):
                            # One broken notification must not block subsequent committed events.
                            logging.getLogger(__name__).error(
                                "Повреждённое общее событие пропущено: %s", identity
                            )
                            hub.cursor = identity
                            continue
                        await hub.deliver(
                            event["room"],
                            {"type": event["type"], "ts": event["ts"], "payload": event["payload"]},
                        )
                        hub.cursor = identity
            except Exception:
                logging.getLogger(__name__).exception("Доставка общих событий временно недоступна")
                await asyncio.sleep(1)
    finally:
        await client.aclose()


async def run_relay() -> None:
    async with asyncio.TaskGroup() as group:
        group.create_task(legacy_relay())
        group.create_task(stream_relay())
