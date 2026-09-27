import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, GenerationJob, SipCall, SystemSetting
from app.operations.backup_protocol import read_signed, write_signed
from app.operations.recovery_compose import SERVICES, recovery_compose
from app.operations.recovery_database import activate_database, prepare_database
from app.operations.recovery_files import stage
from app.operations.snapshot import create_snapshot, file_digest
from app.realtime.clock import tick
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture
from tests.test_program_snapshots import program_fixture
from tests.test_snapshots import fixture_snapshot

PROJECT = "dispatcher_recovery_123456789abc"
SNAPSHOT = "backup-20260926T120000Z-123456789abc"


def recovery_fixture(tmp_path: Path) -> tuple[Path, str, bytes]:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    (config / ".env").write_text(
        "SECRET=private-value\nJWT_SECRET=old-session-key\n", encoding="utf-8"
    )
    program = program_fixture(tmp_path / "program")
    token = config / ".secrets/backup-control/token"
    token.parent.mkdir(parents=True)
    token.write_bytes(b"control-test-key-for-recovery-at-least-32-bytes")
    queue = data / "backup-operations"
    write_signed(
        queue / "schedule.json",
        {"enabled": True, "hour": 7, "minute": 5, "retention": 20},
        token.read_bytes(),
        "schedule",
    )
    (queue / "requests").mkdir()
    (queue / "requests/old.json").write_text("old pending request")
    (data / "telephony").mkdir()
    (data / "telephony/accounts.conf").write_text("old SIP credentials")
    (data / "llm/models").mkdir(parents=True)
    (data / "llm/models/model").write_bytes(b"test-only model")
    value = create_snapshot(backups, data, config, database, key, program_root=program)
    return backups, value["name"], key


def test_recovery_extracts_independent_workspace_and_disarms_old_jobs(tmp_path: Path) -> None:
    backups, name, key = recovery_fixture(tmp_path)
    before = {
        path.relative_to(backups): file_digest(path)
        for path in backups.rglob("*")
        if path.is_file()
    }
    result = stage(backups, name, key, tmp_path / "volume")
    workspace = tmp_path / "volume/workspace"
    assert (workspace / "backend/app/main.py").is_file()
    assert (workspace / "data/llm/models/model").read_bytes() == b"test-only model"
    environment = (workspace / ".env").read_text()
    assert environment.startswith("SECRET=private-value\nJWT_SECRET=")
    assert "old-session-key" not in environment
    assert len(environment.split("JWT_SECRET=", 1)[1].strip()) >= 64
    assert result["session_key_rotated"]
    assert "old-session-key" in (tmp_path / "config/.env").read_text()
    assert (workspace / ".secrets/backup-signing.key").read_bytes() == key
    assert (workspace / "data/telephony/accounts.conf").read_bytes() == b""
    for relative in ("materials", "telephony/speech", "telephony/recordings", "llm/models"):
        assert (workspace / "data" / relative).is_dir()
    assert (workspace / "data/feedback.jsonl").is_file()
    assert not list((workspace / "data/backup-operations/requests").iterdir())
    assert (
        workspace / "data/recovery-history" / name / "backup-operations/requests/old.json"
    ).read_text() == "old pending request"
    schedule = read_signed(
        workspace / "data/backup-operations/schedule.json",
        b"control-test-key-for-recovery-at-least-32-bytes",
        "schedule",
    )
    assert schedule == {"enabled": False, "hour": 7, "minute": 5, "retention": 20}
    assert result["original_schedule"]["enabled"]
    assert before == {
        path.relative_to(backups): file_digest(path)
        for path in backups.rglob("*")
        if path.is_file()
    }
    with pytest.raises(ValueError, match="empty"):
        stage(backups, name, key, tmp_path / "volume")


def test_legacy_or_corrupted_snapshot_cannot_prepare_a_workspace(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    value = create_snapshot(backups, data, config, database, key)
    with pytest.raises(ValueError, match="source"):
        stage(backups, value["name"], key, tmp_path / "volume")
    assert not (tmp_path / "volume").exists()


def composition(root: Path) -> dict[str, Any]:
    services: dict[str, Any] = {
        name: {"environment": {}, "image": "test", "build": {"context": "ignored"}, "volumes": []}
        for name in SERVICES
    }
    services["control"] = {
        "privileged": True,
        "volumes": [{"type": "bind", "source": "/var/run/docker.sock"}],
    }
    services["backend"]["volumes"] = [
        {"type": "bind", "source": str(root / "data"), "target": "/data", "read_only": True},
        {"type": "bind", "source": str(root / "data/materials"), "target": "/data/materials"},
    ]
    services["ollama"]["volumes"] = [
        {"type": "bind", "source": str(root / "data/llm/models"), "target": "/models"}
    ]
    services["backup"]["volumes"] = [
        {"type": "bind", "source": str(root / ".backups"), "target": "/backups"}
    ]
    return {
        "name": "dispatcher112",
        "services": services,
        "volumes": {"postgres_data": {"name": "dispatcher112_postgres_data"}},
        "networks": {"local": {"name": "dispatcher112_local", "internal": True}},
    }


def converted(source: dict[str, Any], root: Path, **changes: Any) -> dict[str, Any]:
    arguments = {
        "project": PROJECT,
        "images": {name: "sha256:" + "a" * 64 for name in SERVICES},
        "ui_port": 15173,
        "api_port": 18000,
        "media_address": "127.0.0.2",
        **changes,
    }
    return recovery_compose(source, root, **arguments)


def test_recovery_compose_separates_all_resources_and_keeps_write_boundaries(
    tmp_path: Path,
) -> None:
    source = composition(tmp_path)
    before = copy.deepcopy(source)
    value = converted(source, tmp_path)
    assert source == before
    assert "control" not in value["services"]
    assert value["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data"
    assert value["networks"]["local"] == {"name": PROJECT + "_local", "internal": True}
    assert all(
        "build" not in item and item["pull_policy"] == "never"
        for item in value["services"].values()
    )
    mounts = value["services"]["backend"]["volumes"]
    assert mounts[0]["read_only"] and mounts[1]["read_only"]
    assert mounts[1]["volume"]["subpath"] == "workspace/data/llm"
    assert mounts[2]["target"] == "/data/materials" and not mounts[2].get("read_only")
    assert value["services"]["backup"]["volumes"][0]["source"] == "recovery_backups"
    assert value["services"]["gateway"]["ports"][0]["published"] == "15173"
    assert {port["host_ip"] for port in value["services"]["telephony"]["ports"]} == {"127.0.0.2"}
    assert (
        value["services"]["ollama"]["volumes"][0]["volume"]["subpath"]
        == "workspace/data/llm/models"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"project": "dispatcher112"},
        {"ui_port": 80},
        {"api_port": 15173},
        {"media_address": "0.0.0.0"},
        {"media_address": "127.0.0.1"},
        {"images": {}},
    ],
)
def test_recovery_rejects_conflicting_or_external_parameters(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        converted(composition(tmp_path), tmp_path, **change)


@pytest.mark.parametrize("change", ["bind", "network", "volume", "privileged", "unknown"])
def test_recovery_rejects_original_resources_or_extra_privileges(
    tmp_path: Path, change: str
) -> None:
    value = composition(tmp_path)
    if change == "bind":
        value["services"]["backend"]["volumes"][0]["source"] = str(
            tmp_path.parent / "original-data"
        )
    elif change == "network":
        value["networks"]["local"]["external"] = True
    elif change == "volume":
        value["volumes"]["postgres_data"]["external"] = True
    elif change == "privileged":
        value["services"]["backend"]["privileged"] = True
    else:
        value["services"]["unrecognized"] = {}
    with pytest.raises(ValueError):
        converted(value, tmp_path)


async def test_recovery_preserves_audit_and_interrupts_only_unfinished_work(
    db: AsyncSession,
) -> None:
    lesson, assignments = await lesson_fixture(db, 2)
    old = AuditLog(action="ORIGINAL_RECOVERY_FIXTURE", payload={"keep": "unchanged"})
    jobs = [
        GenerationJob(lesson_id=lesson.id, user_id=lesson.teacher_id, request={}, status=status)
        for status in ("QUEUED", "RUNNING", "SUCCEEDED")
    ]
    calls = [
        SipCall(
            id=uuid4(),
            user_id=assignments[0].student_id,
            assignment_id=assignments[0].id,
            direction="OUTBOUND",
            state=status,
            media_name="recovery-fixture",
        )
        for status in ("CONNECTED", "ENDED")
    ]
    db.add_all(
        [
            old,
            *jobs,
            *calls,
            SystemSetting(key="maintenance", value={"enabled": True, "job_id": str(uuid4())}),
        ]
    )
    await db.flush()
    value = await prepare_database(db, SNAPSHOT, PROJECT)
    assert value["interrupted_generation"] == 2 and value["interrupted_calls"] == 1
    for row in [old, *jobs, *calls]:
        await db.refresh(row)
    assert old.payload == {"keep": "unchanged"}
    assert [row.status for row in jobs] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert [row.state for row in calls] == ["FAILED", "ENDED"]
    assert calls[0].ended_at is not None and calls[1].ended_at is None
    assert await tick(db, datetime.now(UTC) + timedelta(minutes=3)) == []
    await db.refresh(assignments[0])
    assert assignments[0].state == "QUEUED"
    with pytest.raises(ValueError, match="already"):
        await prepare_database(db, SNAPSHOT, PROJECT)
    activated = await activate_database(db)
    assert activated["status"] == "ACTIVE"
    assert await tick(db, datetime.now(UTC) + timedelta(minutes=3))
    assert await activate_database(db) == activated
    actions = list(
        await db.scalars(
            select(AuditLog.action).where(AuditLog.action.startswith("SYSTEM_RECOVERY_"))
        )
    )
    assert (
        actions.count("SYSTEM_RECOVERY_PREPARED") == actions.count("SYSTEM_RECOVERY_ACTIVATED") == 1
    )
    assert actions.count("SYSTEM_RECOVERY_INTERRUPTED") == 3


@pytest.mark.parametrize(
    "maintenance", [{"enabled": False}, {"enabled": True, "job_id": str(uuid4())}]
)
async def test_recovery_activation_requires_completed_maintenance(
    db: AsyncSession, maintenance: dict[str, Any]
) -> None:
    await prepare_database(db, SNAPSHOT, PROJECT)
    await db.execute(
        text("UPDATE system_settings SET value=CAST(:value AS jsonb) WHERE key='maintenance'"),
        {"value": json.dumps(maintenance)},
    )
    with pytest.raises(ValueError, match="maintenance"):
        await activate_database(db)


async def test_snapshot_of_previously_recovered_installation_can_be_recovered_again(
    db: AsyncSession,
) -> None:
    await prepare_database(db, SNAPSHOT, PROJECT)
    await activate_database(db)
    value = await prepare_database(db, SNAPSHOT, "dispatcher_recovery_abcdef123456")
    assert value["status"] == "PREPARED" and value["instance"] != PROJECT


@pytest.mark.parametrize("already_finished", [False, True])
async def test_recovery_preserves_completed_operation_result(
    db: AsyncSession, already_finished: bool
) -> None:
    identity = uuid4()
    completed = {"status": "SUCCEEDED", "result": {"unchanged": True}}
    db.add(SystemSetting(key="maintenance", value={"enabled": True, "job_id": str(identity)}))
    if already_finished:
        db.add(
            AuditLog(
                action="TECHNICAL_OPERATION_FINISHED",
                entity_type="system",
                entity_id=identity,
                payload=completed,
            )
        )
    await db.flush()
    await prepare_database(db, SNAPSHOT, PROJECT)
    rows = list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == "TECHNICAL_OPERATION_FINISHED", AuditLog.entity_id == identity
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].payload == (
        completed if already_finished else {"status": "FAILED", "reason": "RECOVERY_INTERRUPTED"}
    )


async def test_admin_cannot_open_recovery_before_activation(
    client: AsyncClient, db: AsyncSession
) -> None:
    await prepare_database(db, SNAPSHOT, PROJECT)
    await sign_in(client, "admin", "admin")
    response = await client.post(
        "/api/v1/admin/maintenance",
        json={"enabled": False, "reason": "Попытка преждевременного открытия"},
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "RECOVERY_NOT_ACTIVATED"
    )
