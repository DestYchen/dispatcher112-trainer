"""Apply a committed configuration in each process and publish expiring acknowledgements."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.config import settings
from app.db import base
from app.domain.runtime_configuration import ROLES, RuntimeConfiguration, read_snapshot
from app.transport_security import redis_tls

PREFIX = "runtime-configuration:"
PROCESS_ID = uuid4().hex
current = RuntimeConfiguration()
applied_revision: int | None = None
applied_role: str | None = None
logger = logging.getLogger(__name__)


async def apply_configuration(value: RuntimeConfiguration, role: str) -> None:
    global current
    if role not in ROLES:
        raise ValueError("Unknown runtime process role")
    candidate = base.database_engine(value.database, role)
    try:
        async with candidate.connect() as connection:
            actual = {
                str(name): int(setting)
                for name, setting in (
                    await connection.execute(
                        text(
                            "SELECT name, setting::int FROM pg_settings "
                            "WHERE name IN ('statement_timeout','lock_timeout')"
                        )
                    )
                ).all()
            }
        if actual != {
            "statement_timeout": value.database.statement_timeout_ms,
            "lock_timeout": value.database.lock_timeout_ms,
        }:
            raise ValueError("Database did not apply the requested timeouts")
    except BaseException:
        await candidate.dispose()
        raise
    previous = base.engine
    base.engine = candidate
    base.session_factory.configure(bind=candidate)
    # Checked-out connections finish their existing transactions; new sessions use the new pool.
    await previous.dispose()
    logging.getLogger().setLevel(value.logging.level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(value.logging.level)
    logging.getLogger("app.requests").disabled = not value.logging.http_access
    current = value.model_copy(deep=True)


async def refresh(role: str, cache: Redis) -> None:
    global applied_revision, applied_role
    error = None
    try:
        async with base.session_factory() as db:
            snapshot = await read_snapshot(db)
        if applied_revision != snapshot.revision or applied_role != role:
            await apply_configuration(snapshot.configuration, role)
            applied_revision, applied_role = snapshot.revision, role
    except Exception as failure:
        error = "CONFIGURATION_APPLY_FAILED"
        logger.warning("Configuration refresh failed: %s", type(failure).__name__)
    pool = cast(AsyncAdaptedQueuePool, base.engine.pool)
    value = {
        "id": PROCESS_ID,
        "role": role,
        "revision": applied_revision,
        "at": datetime.now(UTC).isoformat(),
        "error": error,
        "pool_size": pool.size(),
        "checked_out": pool.checkedout(),
        "statement_timeout_ms": current.database.statement_timeout_ms,
        "lock_timeout_ms": current.database.lock_timeout_ms,
    }
    await cache.set(PREFIX + role + ":" + PROCESS_ID, json.dumps(value), ex=15)


async def monitor(role: str, cache: Redis) -> None:
    try:
        while True:
            await asyncio.sleep(2)
            try:
                await refresh(role, cache)
            except Exception as failure:
                logger.warning(
                    "Configuration acknowledgement unavailable: %s", type(failure).__name__
                )
    finally:
        await cache.aclose()


async def start(role: str) -> asyncio.Task[None]:
    cache: Redis = Redis.from_url(settings.redis_url, decode_responses=True, **redis_tls())
    try:
        await refresh(role, cache)
    except BaseException:
        await cache.aclose()
        raise
    return asyncio.create_task(monitor(role, cache))


async def process_status(cache: Redis, revision: int, expected_backends: int = 1) -> dict[str, Any]:
    keys = []
    async for key in cache.scan_iter(match=PREFIX + "*", count=100):
        keys.append(key)
        if len(keys) > 256:
            raise ValueError("Too many runtime configuration acknowledgements")
    processes = []
    rows = await cache.mget(keys) if keys else []
    now = datetime.now(UTC)
    for raw in rows:
        if raw is None:
            continue
        value = json.loads(raw)
        if value.get("role") not in ROLES:
            continue
        age = (now - datetime.fromisoformat(value["at"])).total_seconds()
        if not 0 <= age < 10:
            continue
        processes.append(value)
    present = {value["role"] for value in processes}
    missing = sorted(set(ROLES) - present)
    backend_count = len({value["id"] for value in processes if value["role"] == "backend"})
    if backend_count < expected_backends and "backend" not in missing:
        missing.append("backend")
        missing.sort()
    return {
        "processes": sorted(processes, key=lambda item: (item["role"], item["id"])),
        "missing": missing,
        "applied": not missing
        and all(value["revision"] == revision and value["error"] is None for value in processes),
    }
