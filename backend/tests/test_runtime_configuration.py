import copy
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app import runtime_configuration as runtime
from app.api import admin_configuration as endpoint
from app.config import settings
from app.db import base
from app.db.models import AuditLog, GenerationJob, SipCall, SystemSetting
from app.domain.maintenance import MAINTENANCE_LOCK
from app.domain.runtime_configuration import KEY, ROLES, ConfigurationSnapshot, RuntimeConfiguration
from app.transport_security import redis_tls
from tests.conftest import TEST_URL
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture

PATH = "/api/v1/admin/operations/configuration"


@pytest.fixture
async def cache(client: AsyncClient) -> AsyncIterator[Redis]:
    connection: Redis = Redis.from_url(
        settings.redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True, **redis_tls()
    )
    yield connection
    await connection.aclose()


def change(revision: int = 0) -> dict[str, Any]:
    value = RuntimeConfiguration().model_dump(mode="json")
    value["sip"]["inbound_ring_seconds"] = 25
    return {
        "id": str(uuid4()),
        "expected_revision": revision,
        "configuration": value,
        "reason": "Настройка параметров учебного комплекса",
    }


async def maintenance(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Настройка параметров"}
    )
    assert response.status_code == 200, response.text


async def acknowledge(
    cache: Redis,
    role: str,
    revision: int,
    *,
    age: int = 0,
    error: str | None = None,
    node: str = "node",
) -> None:
    value = {
        "id": node,
        "role": role,
        "revision": revision,
        "error": error,
        "at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        "pool_size": 5,
        "checked_out": 0,
        "statement_timeout_ms": 30000,
        "lock_timeout_ms": 3000,
    }
    await cache.set(runtime.PREFIX + role + ":" + node, json.dumps(value), ex=15)


async def test_save_requires_maintenance_and_preserves_history_and_nonce(
    client: AsyncClient,
    db: AsyncSession,
    cache: Redis,
) -> None:
    await sign_in(client, "admin", "admin")
    initial = (await client.get(PATH)).json()
    assert initial["snapshot"]["revision"] == 0 and not initial["configured"]
    assert set(initial["missing"]) == set(ROLES) and not initial["applied"]
    assert settings.jwt_secret not in json.dumps(initial)
    body = change()
    denied = await client.post(PATH, json=body)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "MAINTENANCE_REQUIRED"
    await maintenance(client)
    saved = await client.post(PATH, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["snapshot"]["revision"] == 1 and not saved.json()["applied"]
    replay = await client.post(PATH, json=body)
    assert replay.json()["replayed"] and replay.json()["snapshot"] == saved.json()["snapshot"]
    wrong = await client.post(PATH, json={**body, "reason": "Подмена причины"})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await client.post(PATH, json=change())
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "CONFIGURATION_CONFLICT"
    denied = await client.post(
        "/api/v1/admin/maintenance", json={"enabled": False, "reason": "Проверка готовности"}
    )
    assert (
        denied.status_code == 409 and denied.json()["error"]["code"] == "CONFIGURATION_NOT_APPLIED"
    )
    for role in ROLES:
        await acknowledge(cache, role, 1)
    assert (await client.get(PATH)).json()["applied"]
    assert (
        await client.post(
            "/api/v1/admin/maintenance", json={"enabled": False, "reason": "Все процессы готовы"}
        )
    ).status_code == 200
    # Losing the successful HTTP response does not require reopening maintenance.
    assert (await client.post(PATH, json=body)).json()["replayed"]
    await maintenance(client)
    newer = change(1)
    newer["configuration"]["sip"]["inbound_ring_seconds"] = 30
    assert (await client.post(PATH, json=newer)).status_code == 200
    old_replay = (await client.post(PATH, json=body)).json()
    assert old_replay["superseded"] and old_replay["saved_revision"] == 1
    assert old_replay["snapshot"]["revision"] == 2
    events = list(
        (
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.action == "RUNTIME_CONFIGURATION_CHANGED")
                .order_by(AuditLog.id)
            )
        ).all()
    )
    assert len(events) == 2
    assert events[0].payload is not None
    assert events[0].payload["previous"]["revision"] == 0
    assert events[0].payload["current"]["configuration"]["sip"]["inbound_ring_seconds"] == 25
    visible = (await client.get("/api/v1/admin/audit")).json()["items"]
    configuration_events = [
        item for item in visible if item["action"] == "RUNTIME_CONFIGURATION_CHANGED"
    ]
    assert len(configuration_events) == 2
    assert all(
        "previous" in item["payload"] and "current" in item["payload"]
        for item in configuration_events
    )


@pytest.mark.parametrize("role,password", [("teacher", "teacher"), ("student1", "student")])
async def test_teaching_roles_cannot_read_or_edit_configuration(
    client: AsyncClient, role: str, password: str
) -> None:
    await sign_in(client, role, password)
    assert (await client.get(PATH)).status_code == 403
    assert (await client.post(PATH, json=change())).status_code == 403


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("database", "api_pool_size", 4),
        ("database", "api_pool_size", 51),
        ("database", "worker_pool_size", 0),
        ("database", "sip_pool_size", 11),
        ("database", "max_overflow", -1),
        ("database", "pool_timeout_seconds", 0),
        ("database", "statement_timeout_ms", 499),
        ("database", "statement_timeout_ms", 30001),
        ("database", "lock_timeout_ms", 99),
        ("database", "api_pool_size", True),
        ("database", "password", "not-an-editable-setting"),
        ("sip", "inbound_ring_seconds", 61),
        ("sip", "unanswered_seconds", 24),
        ("sip", "max_call_seconds", 59),
        ("logging", "level", "DEBUG"),
        ("logging", "http_access", "false"),
    ],
)
async def test_invalid_settings_never_change_configuration(
    client: AsyncClient, db: AsyncSession, group: str, field: str, value: Any
) -> None:
    await sign_in(client, "admin", "admin")
    await maintenance(client)
    body = change()
    body["configuration"][group][field] = value
    response = await client.post(PATH, json=body)
    assert response.status_code == 400, response.text
    assert await db.get(SystemSetting, KEY) is None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "RUNTIME_CONFIGURATION_CHANGED")
        )
        == 0
    )


async def test_database_budget_and_owned_maintenance_are_enforced(
    client: AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(client, "admin", "admin")
    await maintenance(client)

    async def limited_budget(db: AsyncSession) -> int:
        return 30

    monkeypatch.setattr(endpoint, "connection_budget", limited_budget)
    response = await client.post(PATH, json=change())
    assert (
        response.status_code == 400 and response.json()["error"]["code"] == "CONFIGURATION_CAPACITY"
    )
    row = await db.get(SystemSetting, "maintenance")
    assert row is not None
    row.value = {**row.value, "update_id": str(uuid4())}
    await db.commit()
    response = await client.post(PATH, json=change())
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "OPERATION_IN_PROGRESS"
    )


@pytest.mark.parametrize("mode", ["missing", "old", "error", "stale", "future", "second_node"])
async def test_all_fresh_processes_must_acknowledge_the_revision(cache: Redis, mode: str) -> None:
    for role in ROLES:
        await acknowledge(cache, role, 7)
    assert (await runtime.process_status(cache, 7))["applied"]
    if mode == "missing":
        await cache.delete(runtime.PREFIX + "worker:node")
    else:
        await acknowledge(
            cache,
            "worker",
            6 if mode in {"old", "second_node"} else 7,
            age=11 if mode == "stale" else -60 if mode == "future" else 0,
            error="CONFIGURATION_APPLY_FAILED" if mode == "error" else None,
            node="second" if mode == "second_node" else "node",
        )
    assert not (await runtime.process_status(cache, 7))["applied"]


@pytest.mark.parametrize("second", ["missing", "stale", "old", "current"])
async def test_every_configured_backend_must_acknowledge(cache: Redis, second: str) -> None:
    for role in ROLES:
        await acknowledge(cache, role, 7)
    if second != "missing":
        await acknowledge(
            cache,
            "backend",
            6 if second == "old" else 7,
            node="second",
            age=11 if second == "stale" else 0,
        )
    result = await runtime.process_status(cache, 7, expected_backends=2)
    assert result["applied"] is (second == "current")
    if second in {"missing", "stale"}:
        assert "backend" in result["missing"]


@pytest.fixture
async def isolated_runtime(
    prepared_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    engine = create_async_engine(TEST_URL)
    monkeypatch.setattr(base, "engine", engine)
    monkeypatch.setattr(base, "session_factory", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "database_url", TEST_URL)
    monkeypatch.setattr(runtime, "current", RuntimeConfiguration())
    monkeypatch.setattr(runtime, "applied_revision", None)
    monkeypatch.setattr(runtime, "applied_role", None)
    root = logging.getLogger()
    previous_level = root.level
    handler_levels = [(handler, handler.level) for handler in root.handlers]
    disabled = logging.getLogger("app.requests").disabled
    try:
        yield
    finally:
        await base.engine.dispose()
        await engine.dispose()
        root.setLevel(previous_level)
        for handler, level in handler_levels:
            handler.setLevel(level)
        logging.getLogger("app.requests").disabled = disabled


async def test_new_pool_enforces_real_timeouts_without_killing_existing_transactions(
    isolated_runtime: None,
) -> None:
    value = RuntimeConfiguration()
    value.database.statement_timeout_ms = 500
    value.database.lock_timeout_ms = 100
    value.logging.level = "WARNING"
    value.logging.http_access = False
    lock = 991123
    async with base.session_factory() as old:
        previous_pid = await old.scalar(text("SELECT pg_backend_pid()"))
        await old.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock})
        await runtime.apply_configuration(value, "backend")
        assert await old.scalar(text("SELECT 1")) == 1
        async with base.session_factory() as new:
            assert await new.scalar(text("SELECT pg_backend_pid()")) != previous_pid
            with pytest.raises(DBAPIError) as failure:
                await new.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock})
            assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
    async with base.session_factory() as new:
        with pytest.raises(DBAPIError) as failure:
            await new.execute(text("SELECT pg_sleep(0.8)"))
        assert getattr(failure.value.orig, "sqlstate", None) == "57014"
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("app.requests").disabled
    assert runtime.current.sip == value.sip


async def test_pool_size_overflow_and_wait_timeout_are_effective(isolated_runtime: None) -> None:
    value = RuntimeConfiguration()
    value.database.worker_pool_size = 1
    value.database.max_overflow = 1
    value.database.pool_timeout_seconds = 1
    await runtime.apply_configuration(value, "worker")
    pool = cast(AsyncAdaptedQueuePool, base.engine.pool)
    async with base.engine.connect(), base.engine.connect():
        assert pool.size() == 1 and pool.checkedout() == 2 and pool.overflow() == 1
        with pytest.raises(PoolTimeout):
            await base.engine.connect()


async def test_failed_candidate_keeps_the_previous_pool_and_runtime_values(
    isolated_runtime: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = base.engine
    original = copy.deepcopy(runtime.current)
    bad = RuntimeConfiguration()
    bad.logging.level = "ERROR"
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://invalid@127.0.0.1:1/unavailable"
    )
    with pytest.raises(OSError):
        await runtime.apply_configuration(bad, "backend")
    assert base.engine is previous and runtime.current == original
    async with base.session_factory() as db:
        assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize(
    "kind,state",
    [
        ("lesson", "RUNNING"),
        ("generation", "QUEUED"),
        ("generation", "RUNNING"),
        ("sip", "REQUESTED"),
        ("sip", "RINGING"),
        ("sip", "CONNECTED"),
    ],
)
async def test_active_work_blocks_configuration(
    client: AsyncClient,
    db: AsyncSession,
    kind: str,
    state: str,
) -> None:
    await sign_in(client, "admin", "admin")
    await maintenance(client)
    lesson, assignments = await lesson_fixture(db, 1)
    if kind != "lesson":
        lesson.status = "FINISHED"
    if kind == "generation":
        db.add(
            GenerationJob(lesson_id=lesson.id, user_id=lesson.teacher_id, status=state, request={})
        )
    if kind == "sip":
        db.add(
            SipCall(
                user_id=assignments[0].student_id,
                assignment_id=assignments[0].id,
                direction="INBOUND",
                media_name="0" * 64,
                state=state,
            )
        )
    await db.flush()
    response = await client.post(PATH, json=change())
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "OPERATION_IN_PROGRESS"
    )
    assert await db.get(SystemSetting, KEY) is None


async def test_csrf_and_short_reasons_cannot_change_configuration(client: AsyncClient) -> None:
    await sign_in(client, "admin", "admin")
    response = await client.post(PATH, json=change(), headers={"X-CSRF-Token": "wrong"})
    assert response.status_code == 403
    response = await client.post(PATH, json={**change(), "reason": "     "})
    assert response.status_code == 400


async def test_concurrent_maintenance_operation_prevents_save(client: AsyncClient) -> None:
    await sign_in(client, "admin", "admin")
    engine = create_async_engine(
        TEST_URL, connect_args={"server_settings": {"statement_timeout": "5000"}}
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:id)"), {"id": MAINTENANCE_LOCK}
            )
            response = await client.post(PATH, json=change())
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "OPERATION_IN_PROGRESS"
    finally:
        await engine.dispose()


async def test_unavailable_acknowledgements_do_not_allow_leaving_maintenance(
    client: AsyncClient,
    cache: Redis,
) -> None:
    await sign_in(client, "admin", "admin")
    await maintenance(client)
    assert (await client.post(PATH, json=change())).status_code == 200
    await cache.set(runtime.PREFIX + "worker:invalid", "not-json", ex=15)
    response = await client.get(PATH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONFIGURATION_STATUS_UNAVAILABLE"
    response = await client.post(
        "/api/v1/admin/maintenance", json={"enabled": False, "reason": "Завершение настройки"}
    )
    assert response.status_code == 503
    assert (await client.get("/api/v1/admin/maintenance")).json()["enabled"]


async def test_refresh_acknowledges_only_effective_revision_and_retries_failure(
    isolated_runtime: None,
    cache: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = ConfigurationSnapshot(revision=3)
    snapshot.configuration.database.worker_pool_size = 2
    snapshot.configuration.database.statement_timeout_ms = 4000
    snapshot.configuration.database.lock_timeout_ms = 200

    async def read_snapshot(db: AsyncSession) -> ConfigurationSnapshot:
        return snapshot

    monkeypatch.setattr(runtime, "read_snapshot", read_snapshot)
    await runtime.refresh("worker", cache)
    raw = await cache.get(runtime.PREFIX + "worker:" + runtime.PROCESS_ID)
    acknowledged = json.loads(raw)
    assert acknowledged["revision"] == 3 and acknowledged["error"] is None
    assert acknowledged["pool_size"] == 2 and acknowledged["statement_timeout_ms"] == 4000
    assert 0 < await cache.ttl(runtime.PREFIX + "worker:" + runtime.PROCESS_ID) <= 15
    original = base.engine
    await runtime.refresh("worker", cache)
    assert base.engine is original
    snapshot.revision = 4
    snapshot.configuration.database.worker_pool_size = 3
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://invalid@127.0.0.1:1/unavailable"
    )
    await runtime.refresh("worker", cache)
    acknowledged = json.loads(await cache.get(runtime.PREFIX + "worker:" + runtime.PROCESS_ID))
    assert acknowledged["revision"] == 3 and acknowledged["error"] == "CONFIGURATION_APPLY_FAILED"
    assert acknowledged["pool_size"] == 2 and base.engine is original
    monkeypatch.setattr(settings, "database_url", TEST_URL)
    await runtime.refresh("worker", cache)
    acknowledged = json.loads(await cache.get(runtime.PREFIX + "worker:" + runtime.PROCESS_ID))
    assert acknowledged["revision"] == 4 and acknowledged["error"] is None
    assert acknowledged["pool_size"] == 3 and base.engine is not original
