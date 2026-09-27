import copy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import AuditLog, GenerationJob, SipCall, User
from app.domain.maintenance import MAINTENANCE_LOCK
from app.operations import update_database as updates
from app.operations.update_files import read_record, write_record
from tests.conftest import TEST_URL
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture

PHASES = (
    "STARTED",
    "BACKED_UP",
    "MIGRATED",
    "READY",
    "ACTIVE",
    "ROLLING_BACK",
    "ROLLED_BACK",
    "FAILED",
)
ALLOWED = {
    ("STARTED", "BACKED_UP"),
    ("STARTED", "ROLLING_BACK"),
    ("STARTED", "FAILED"),
    ("BACKED_UP", "MIGRATED"),
    ("BACKED_UP", "ROLLING_BACK"),
    ("BACKED_UP", "FAILED"),
    ("MIGRATED", "READY"),
    ("MIGRATED", "ROLLING_BACK"),
    ("MIGRATED", "FAILED"),
    ("READY", "ACTIVE"),
    ("READY", "ROLLING_BACK"),
    ("READY", "FAILED"),
    ("ROLLING_BACK", "ROLLED_BACK"),
    ("ROLLING_BACK", "FAILED"),
    ("FAILED", "ROLLING_BACK"),
}
DETAILS: dict[str, dict[str, Any]] = {
    "BACKED_UP": {"snapshot": "backup-20260926T190000Z-abcdef012345"},
    "MIGRATED": {"revision": "abc123"},
    "READY": {"health_checked": True},
    "FAILED": {"error_code": "MIGRATION_FAILED"},
}


async def admin(db: AsyncSession) -> UUID:
    identity = await db.scalar(select(User.id).where(User.login == "admin"))
    assert identity is not None
    return identity


async def begin(db: AsyncSession, actor: UUID) -> dict[str, Any]:
    return await updates.begin_update(db, uuid4(), actor, "1.2.3", "a" * 64, "Плановое обновление")


async def test_status_distinguishes_current_historical_and_absent_updates(db: AsyncSession) -> None:
    actor = await admin(db)
    absent = uuid4()
    assert await updates.update_status(db, absent, actor) == {"id": str(absent), "phase": "ABSENT"}
    value = await begin(db, actor)
    identity = UUID(value["id"])
    assert await updates.update_status(db, identity, actor) == {
        "id": str(identity),
        "phase": "STARTED",
    }
    for phase in ("BACKED_UP", "MIGRATED", "READY", "ACTIVE"):
        await updates.advance_update(db, identity, actor, phase, DETAILS.get(phase, {}))
    await begin(db, actor)
    previous = await updates.update_status(db, identity, actor)
    assert previous["id"] == str(identity) and previous["phase"] == "ACTIVE"
    assert await updates.update_status(db, absent, actor) == {"id": str(absent), "phase": "ABSENT"}


@pytest.mark.parametrize("login", ["teacher", "student1", "inactive", "absent"])
async def test_status_requires_an_active_administrator(db: AsyncSession, login: str) -> None:
    identity = UUID((await begin(db, await admin(db)))["id"])
    if login == "absent":
        actor = uuid4()
    else:
        user = await db.scalar(
            select(User).where(User.login == ("admin" if login == "inactive" else login))
        )
        assert user is not None
        if login == "inactive":
            user.is_active = False
            await db.flush()
        actor = user.id
    with pytest.raises(ValueError, match="active administrator"):
        await updates.update_status(db, identity, actor)


async def test_update_is_owned_idempotent_and_maintains_original_state(db: AsyncSession) -> None:
    actor = await admin(db)
    previous = {"enabled": True, "reason": "Обслуживание до обновления"}
    await updates.save_setting(db, "maintenance", previous, actor)
    value = await begin(db, actor)
    identity = UUID(value["id"])
    assert (
        await updates.begin_update(db, identity, actor, "1.2.3", "a" * 64, "Плановое обновление")
        == value
    )
    with pytest.raises(ValueError, match="different request"):
        await updates.begin_update(db, identity, actor, "1.2.4", "a" * 64, "Плановое обновление")
    with pytest.raises(ValueError, match="incomplete"):
        await begin(db, actor)
    for phase in ("BACKED_UP", "MIGRATED", "READY", "ACTIVE"):
        current = await updates.advance_update(db, identity, actor, phase, DETAILS.get(phase, {}))
        assert (
            await updates.advance_update(db, identity, actor, phase, DETAILS.get(phase, {}))
            == current
        )
    assert await updates.setting(db, "maintenance") == previous
    assert (
        await db.scalar(
            text("SELECT count(*) FROM audit_log WHERE action='SOFTWARE_UPDATE_STARTED'")
        )
        == 1
    )
    assert (await begin(db, actor))["id"] != value["id"]
    with pytest.raises(ValueError, match="incomplete"):
        await updates.begin_update(db, identity, actor, "1.2.3", "a" * 64, "Плановое обновление")


@pytest.mark.parametrize("current", PHASES)
@pytest.mark.parametrize("target", PHASES)
async def test_all_update_phase_pairs(db: AsyncSession, current: str, target: str) -> None:
    actor = await admin(db)
    value = await begin(db, actor)
    value.update(phase=current, phase_details=DETAILS.get(current, {}))
    await updates.save_setting(db, "software_update", value, actor)
    if current == target or (current, target) in ALLOWED:
        result = await updates.advance_update(
            db, UUID(value["id"]), actor, target, DETAILS.get(target, {})
        )
        assert result["phase"] == target
        maintenance = await updates.setting(db, "maintenance")
        if current != target and target in ("ACTIVE", "ROLLED_BACK"):
            assert maintenance == value["previous_maintenance"]
        else:
            assert maintenance["enabled"]
    else:
        with pytest.raises(ValueError, match="transition"):
            await updates.advance_update(
                db, UUID(value["id"]), actor, target, DETAILS.get(target, {})
            )
        assert (await updates.setting(db, "software_update"))["phase"] == current


@pytest.mark.parametrize("login", ["teacher", "student1", "inactive", "absent"])
async def test_only_active_admin_can_start_update(db: AsyncSession, login: str) -> None:
    if login == "absent":
        actor = uuid4()
    else:
        user = await db.scalar(
            select(User).where(User.login == ("admin" if login == "inactive" else login))
        )
        assert user is not None
        if login == "inactive":
            user.is_active = False
            await db.flush()
        actor = user.id
    with pytest.raises(ValueError, match="active administrator"):
        await begin(db, actor)
    assert await updates.setting(db, "software_update") == {}


@pytest.mark.parametrize(
    "busy", ["lesson", "queued", "generation", "call", "operation", "recovery"]
)
async def test_busy_installation_is_not_modified(db: AsyncSession, busy: str) -> None:
    actor = await admin(db)
    if busy in ("lesson", "queued", "generation", "call"):
        lesson, assignments = await lesson_fixture(db, 1)
        if busy != "lesson":
            lesson.status = "FINISHED"
        if busy in ("queued", "generation"):
            db.add(
                GenerationJob(
                    lesson_id=lesson.id,
                    user_id=lesson.teacher_id,
                    request={},
                    status="QUEUED" if busy == "queued" else "RUNNING",
                )
            )
        if busy == "call":
            db.add(
                SipCall(
                    id=uuid4(),
                    user_id=assignments[0].student_id,
                    assignment_id=assignments[0].id,
                    direction="OUTBOUND",
                    state="CONNECTED",
                    media_name="test",
                )
            )
        await db.flush()
    elif busy == "operation":
        await updates.save_setting(
            db, "maintenance", {"enabled": True, "job_id": str(uuid4())}, actor
        )
    else:
        await updates.save_setting(db, "recovery", {"status": "PREPARED"}, actor)
    before = await updates.setting(db, "maintenance")
    with pytest.raises(ValueError):
        await begin(db, actor)
    assert await updates.setting(db, "software_update") == {}
    assert await updates.setting(db, "maintenance") == before


@pytest.mark.parametrize(
    "phase,details",
    [
        ("BACKED_UP", {"snapshot": "../backup"}),
        ("MIGRATED", {"revision": "bad revision"}),
        ("READY", {"health_checked": False}),
        ("FAILED", {"error_code": "password=private"}),
        ("BACKED_UP", {"snapshot": DETAILS["BACKED_UP"]["snapshot"], "command": "arbitrary"}),
    ],
)
async def test_phase_requires_bounded_evidence(
    db: AsyncSession, phase: str, details: dict[str, Any]
) -> None:
    actor = await admin(db)
    value = await begin(db, actor)
    value["phase"] = {"MIGRATED": "BACKED_UP", "READY": "MIGRATED"}.get(phase, "STARTED")
    await updates.save_setting(db, "software_update", value, actor)
    with pytest.raises(ValueError):
        await updates.advance_update(db, UUID(value["id"]), actor, phase, details)
    assert (await updates.setting(db, "software_update"))["phase"] == value["phase"]


async def rolling_tail(db: AsyncSession, actor: UUID) -> tuple[UUID, dict[str, Any]]:
    original = AuditLog(action="EXISTING_HISTORY", payload={"immutable": "исходная запись"})
    db.add(original)
    await db.flush()
    value = await begin(db, actor)
    identity = UUID(value["id"])
    restored_snapshot = await db.begin_nested()
    await updates.advance_update(db, identity, actor, "BACKED_UP", DETAILS["BACKED_UP"])
    db.add(AuditLog(user_id=actor, action="LOGIN", payload={"after": "backup"}, ip="127.0.0.1"))
    await db.flush()
    await updates.advance_update(db, identity, actor, "ROLLING_BACK", {})
    tail = await updates.export_audit_tail(db, identity, actor)
    # PostgreSQL savepoint models restoring the earlier snapshot without disabling audit protection.
    await restored_snapshot.rollback()
    assert (await updates.setting(db, "software_update"))["phase"] == "STARTED"
    return identity, tail


async def test_rollback_preserves_exact_audit_rows_and_is_repeatable(db: AsyncSession) -> None:
    actor = await admin(db)
    identity, tail = await rolling_tail(db, actor)
    result = await updates.merge_audit_tail(db, identity, actor, tail)
    assert result == {"imported": len(tail["rows"]) - 1, "already_present": 1}
    for row in tail["rows"]:
        actual = await db.scalar(
            text("SELECT to_jsonb(a) FROM audit_log a WHERE id=:id"), {"id": row["id"]}
        )
        assert updates.audit_row(actual) == row
    again = await updates.merge_audit_tail(db, identity, actor, tail)
    assert again == {"imported": 0, "already_present": len(tail["rows"])}
    assert (
        await db.scalar(
            text("SELECT count(*) FROM audit_log WHERE action='SOFTWARE_UPDATE_AUDIT_RESTORED'")
        )
        == 1
    )
    next_row = AuditLog(action="AFTER_ROLLBACK", payload={})
    db.add(next_row)
    await db.flush()
    assert next_row.id > max(row["id"] for row in tail["rows"])
    await updates.advance_update(db, identity, actor, "ROLLED_BACK", {})
    assert not (await updates.setting(db, "maintenance"))["enabled"]
    async with db.begin_nested() as protection:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(
                text("UPDATE audit_log SET action='EDITED' WHERE id=:id"), {"id": next_row.id}
            )
        await protection.rollback()


@pytest.mark.parametrize(
    "change", ["conflict", "actor", "order", "duplicate", "identity", "anchor", "timezone"]
)
async def test_bad_tail_changes_nothing(db: AsyncSession, change: str) -> None:
    actor = await admin(db)
    identity, valid = await rolling_tail(db, actor)
    tail = copy.deepcopy(valid)
    if change == "conflict":
        tail["rows"][0]["action"] = "DIFFERENT_EXISTING_ROW"
    elif change == "actor":
        tail["rows"][-1]["user_id"] = str(uuid4())
    elif change == "order":
        tail["rows"].reverse()
    elif change == "duplicate":
        tail["rows"].append(tail["rows"][-1])
    elif change == "identity":
        tail["update"]["id"] = str(uuid4())
    elif change == "anchor":
        tail["update"]["audit_anchor"]["sha256"] = "0" * 64
    else:
        tail["rows"][-1]["created_at"] = "2026-09-26T19:00:00"
    before = await db.scalar(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ValueError):
        await updates.merge_audit_tail(db, identity, actor, tail)
    assert await db.scalar(text("SELECT count(*) FROM audit_log")) == before
    assert (await updates.setting(db, "software_update"))["phase"] == "STARTED"


async def test_http_cannot_release_update_maintenance_or_start_parallel_operation(
    db: AsyncSession, client: AsyncClient
) -> None:
    await sign_in(client, "admin", "admin")
    await begin(db, await admin(db))
    for enabled in (True, False):
        result = await client.post(
            "/api/v1/admin/maintenance", json={"enabled": enabled, "reason": "Попытка обхода"}
        )
        assert (
            result.status_code == 409 and result.json()["error"]["code"] == "OPERATION_IN_PROGRESS"
        )
    result = await client.post(
        "/api/v1/admin/operations/jobs",
        json={
            "id": str(uuid4()),
            "kind": "service",
            "service": "worker",
            "action": "restart",
        },
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "OPERATION_IN_PROGRESS"
    assert (await client.patch("/api/v1/admin/policies", json={})).status_code == 503


async def test_audit_file_must_have_matching_installation_signature(
    db: AsyncSession, tmp_path: Path
) -> None:
    actor = await admin(db)
    identity, tail = await rolling_tail(db, actor)
    path = tmp_path / "audit.json"
    key = b"installation-audit-signing-key-32-bytes"
    write_record(path, tail, key, "software-update-audit-1")
    before = await db.scalar(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ValueError, match="signature"):
        await updates.merge_audit_file(
            db, identity, actor, path, b"other-installation-key-32-bytes-long"
        )
    assert await db.scalar(text("SELECT count(*) FROM audit_log")) == before
    assert (await updates.merge_audit_file(db, identity, actor, path, key))["imported"] > 0


async def test_audit_export_is_signed_and_does_not_overwrite_evidence(
    db: AsyncSession, tmp_path: Path
) -> None:
    actor = await admin(db)
    value = await begin(db, actor)
    identity = UUID(value["id"])
    await updates.advance_update(db, identity, actor, "ROLLING_BACK", {})
    key = b"installation-audit-signing-key-32-bytes"
    path = tmp_path / "audit.json"
    result = await updates.export_audit_file(db, identity, actor, path, key)
    assert result["exported"] == 2
    saved = path.read_bytes()
    assert len(read_record(path, key, "software-update-audit-1")["rows"]) == 2
    with pytest.raises(ValueError, match="already exists"):
        await updates.export_audit_file(db, identity, actor, path, key)
    assert path.read_bytes() == saved


@pytest.mark.parametrize("details", [None, [], True, 42])
async def test_cli_json_details_must_be_an_object(db: AsyncSession, details: Any) -> None:
    actor = await admin(db)
    value = await begin(db, actor)
    with pytest.raises(ValueError, match="must be an object"):
        await updates.advance_update(db, UUID(value["id"]), actor, "ROLLING_BACK", details)
    assert (await updates.setting(db, "software_update"))["phase"] == "STARTED"


@pytest.mark.parametrize("field", ["user_id", "entity_id", "ip"])
async def test_malformed_audit_scalar_fails_before_any_insert(db: AsyncSession, field: str) -> None:
    actor = await admin(db)
    identity, tail = await rolling_tail(db, actor)
    tail["rows"][-1][field] = 42
    before = await db.scalar(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ValueError, match="strings"):
        await updates.merge_audit_tail(db, identity, actor, tail)
    assert await db.scalar(text("SELECT count(*) FROM audit_log")) == before


@pytest.mark.parametrize("limit", ["MAX_TAIL_ROWS", "MAX_TAIL_BYTES"])
async def test_audit_limits_refuse_whole_transfer_without_truncation(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    actor = await admin(db)
    identity, tail = await rolling_tail(db, actor)
    monkeypatch.setattr(updates, limit, 1)
    before = await db.scalar(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ValueError, match="size"):
        await updates.merge_audit_tail(db, identity, actor, tail)
    assert await db.scalar(text("SELECT count(*) FROM audit_log")) == before
    await updates.advance_update(db, identity, actor, "ROLLING_BACK", {})
    before = await db.scalar(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ValueError, match="limit"):
        await updates.export_audit_tail(db, identity, actor)
    assert await db.scalar(text("SELECT count(*) FROM audit_log")) == before


async def test_matching_metadata_cannot_hide_a_different_original_audit_prefix(
    db: AsyncSession,
) -> None:
    actor = await admin(db)
    identity, tail = await rolling_tail(db, actor)
    current = await updates.setting(db, "software_update")
    current["audit_anchor"]["sha256"] = "0" * 64
    tail["update"]["audit_anchor"]["sha256"] = "0" * 64
    await updates.save_setting(db, "software_update", current, actor)
    before = await db.scalar(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ValueError, match="prefix"):
        await updates.merge_audit_tail(db, identity, actor, tail)
    assert await db.scalar(text("SELECT count(*) FROM audit_log")) == before


async def test_active_application_transaction_prevents_starting_update(db: AsyncSession) -> None:
    actor = await admin(db)
    engine = create_async_engine(TEST_URL, poolclass=NullPool)
    try:
        async with engine.connect() as other:
            await other.execute(
                text("SELECT pg_advisory_lock_shared(:key)"), {"key": MAINTENANCE_LOCK}
            )
            try:
                with pytest.raises(ValueError, match="transaction"):
                    await begin(db, actor)
                assert await updates.setting(db, "software_update") == {}
            finally:
                await other.execute(
                    text("SELECT pg_advisory_unlock_shared(:key)"), {"key": MAINTENANCE_LOCK}
                )
    finally:
        await engine.dispose()
