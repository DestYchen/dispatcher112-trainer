import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_operations import reconcile_pending_operation
from app.db.models import AuditLog
from app.operations import backup_client, backup_worker
from app.operations.backup_protocol import (
    DEFAULT_SCHEDULE,
    enqueue,
    read_job,
    read_signed,
    write_signed,
)
from app.operations.snapshot import create_snapshot
from tests.test_auth import sign_in
from tests.test_program_snapshots import program_fixture

KEY = b"test-only-executor-signing-key-with-sufficient-length"


@pytest.fixture
def backup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    queue, status, backups = tmp_path / "queue", tmp_path / "status", tmp_path / "backups"
    token = tmp_path / "token"
    token.write_bytes(KEY)
    signing = tmp_path / "snapshot-key"
    signing.write_bytes(b"private-snapshot-key")
    for module in (backup_worker, backup_client):
        monkeypatch.setattr(module, "ROOT", queue)
        monkeypatch.setattr(module, "STATUS", status)
        monkeypatch.setattr(module, "TOKEN", token)
    monkeypatch.setattr(backup_worker, "BACKUPS", backups)
    monkeypatch.setattr(backup_worker, "SIGNING_KEY", signing)
    data, config = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    config.mkdir()
    (config / ".env").write_text("PASSWORD=do-not-publish")
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"test-dump-bytes")
    snapshot = create_snapshot(backups, data, config, dump, signing.read_bytes())
    write_signed(status / "worker.json", {"at": datetime.now(UTC).isoformat()}, KEY, "heartbeat")
    backup_worker.publish_catalog(KEY)
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> dict[str, Any]:
        calls.append(args)
        return {"snapshot": snapshot["name"], "files": 2, "source_unchanged": True}

    monkeypatch.setattr(backup_worker, "run_backup", run)
    return {"root": queue, "status": status, "snapshot": snapshot["name"], "calls": calls}


def body(kind: str = "backup_create", **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "actor_id": str(uuid4()),
        "kind": kind,
        "service": "backup",
        **extra,
    }


def test_executor_runs_one_fixed_operation_and_ignores_completed_retry(
    backup_environment: dict[str, Any],
) -> None:
    request = body("backup_verify", snapshot=backup_environment["snapshot"])
    enqueue(backup_environment["root"], request, KEY)
    assert backup_worker.process_one(KEY)
    assert not backup_worker.process_one(KEY)
    assert backup_environment["calls"] == [("--verify", backup_environment["snapshot"])]
    assert read_job(backup_environment["root"], UUID(request["id"]), KEY)["status"] == "SUCCEEDED"


def test_configuration_changes_are_persistent_and_do_not_delete_snapshots(
    backup_environment: dict[str, Any],
) -> None:
    policy = {"enabled": False, "hour": 22, "minute": 45, "retention": 30}
    enqueue(backup_environment["root"], body("backup_configure", schedule=policy), KEY)
    backup_worker.process_one(KEY)
    assert backup_worker.schedule(KEY) == policy
    assert not backup_environment["calls"]
    assert len(list((backup_worker.BACKUPS / "snapshots").iterdir())) == 1


def test_executor_restart_marks_incomplete_job_failed_without_repeating(
    backup_environment: dict[str, Any],
) -> None:
    request = body()
    enqueue(backup_environment["root"], request, KEY)
    write_signed(
        backup_environment["root"] / "results" / f"{request['id']}.json",
        {"id": request["id"], "status": "RUNNING", "request": request},
        KEY,
        "result",
    )
    backup_worker.recover(KEY)
    assert not backup_worker.process_one(KEY)
    result = read_job(backup_environment["root"], UUID(request["id"]), KEY)
    assert result["status"] == "FAILED" and "перезапущен" in result["error"]
    assert not backup_environment["calls"]


def test_failed_backup_does_not_report_success_or_expose_diagnostics(
    backup_environment: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: str) -> dict[str, Any]:
        raise RuntimeError("private-diagnostic-value")

    monkeypatch.setattr(backup_worker, "run_backup", failed)
    request = body()
    enqueue(backup_environment["root"], request, KEY)
    backup_worker.process_one(KEY)
    result = read_job(backup_environment["root"], UUID(request["id"]), KEY)
    assert result["status"] == "FAILED"
    assert "private-diagnostic-value" not in json.dumps(result)


def test_worker_does_not_execute_an_unsupported_signed_request(
    backup_environment: dict[str, Any],
) -> None:
    request = body("shell", command="do-not-execute")
    write_signed(
        backup_environment["root"] / "requests" / f"{request['id']}.json", request, KEY, "request"
    )
    assert not backup_worker.process_one(KEY)
    assert not backup_environment["calls"]


def test_per_snapshot_verification_survives_other_checks_and_rejects_tampering(
    backup_environment: dict[str, Any],
) -> None:
    name = backup_environment["snapshot"]
    verified = {
        "snapshot": name,
        "verified_at": datetime.now(UTC).isoformat(),
        "restored_users": 10,
        "source_unchanged": True,
    }
    path = backup_worker.BACKUPS / "verifications" / f"{name}.json"
    write_signed(path, verified, KEY, "verification")
    (backup_worker.BACKUPS / "verification.json").write_text(json.dumps({"snapshot": "another"}))
    backup_worker.publish_catalog(KEY)
    catalog = read_signed(backup_environment["status"] / "catalog.json", KEY, "catalog")
    assert catalog["items"][0]["verification"] == verified
    envelope = json.loads(path.read_text())
    envelope["value"]["restored_users"] = 100
    path.write_text(json.dumps(envelope))
    backup_worker.publish_catalog(KEY)
    catalog = read_signed(backup_environment["status"] / "catalog.json", KEY, "catalog")
    assert catalog["items"][0]["verification"] is None


def test_catalog_distinguishes_source_backups_and_legacy_without_publishing_paths(
    backup_environment: dict[str, Any],
    tmp_path: Path,
) -> None:
    program = program_fixture(tmp_path / "program")
    current = create_snapshot(
        backup_worker.BACKUPS,
        tmp_path / "data",
        tmp_path / "config",
        tmp_path / "database.dump",
        backup_worker.SIGNING_KEY.read_bytes(),
        program_root=program,
    )
    backup_worker.publish_catalog(KEY)
    catalog = read_signed(backup_environment["status"] / "catalog.json", KEY, "catalog")
    by_name = {item["name"]: item for item in catalog["items"]}
    assert by_name[backup_environment["snapshot"]]["program_files"] == 0
    assert by_name[current["name"]]["program_files"] == current["program_files"]
    assert by_name[current["name"]]["schema"] == "dispatcher-backup-2"
    assert "PASSWORD" not in json.dumps(catalog) and "main.py" not in json.dumps(catalog)


async def test_backup_api_runs_without_docker_controller_and_audits_once(
    client: httpx.AsyncClient,
    db: AsyncSession,
    backup_environment: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Backup must not call the Docker controller")

    monkeypatch.setattr("app.api.admin_operations.control_request", forbidden)
    await sign_in(client, "admin", "admin")
    request = {"id": str(uuid4()), "kind": "backup_create", "service": "backup"}
    assert (await client.post("/api/v1/admin/operations/jobs", json=request)).status_code == 409
    await client.post(
        "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Проверка копии"}
    )
    for _ in range(2):
        response = await client.post("/api/v1/admin/operations/jobs", json=request)
        assert response.status_code == 202 and response.json()["status"] == "QUEUED"
    await asyncio.to_thread(backup_worker.process_one, KEY)
    # Closing the browser must not prevent the audit or keep the operation slot busy.
    await reconcile_pending_operation(db)
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "TECHNICAL_OPERATION_FINISHED",
                AuditLog.entity_id == UUID(request["id"]),
            )
        )
        == 1
    )
    for _ in range(2):
        response = await client.get("/api/v1/admin/operations/jobs/" + request["id"])
        assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED"
    assert backup_environment["calls"] == [()]
    for action in ("TECHNICAL_OPERATION_REQUESTED", "TECHNICAL_OPERATION_FINISHED"):
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == action, AuditLog.entity_id == UUID(request["id"]))
            )
            == 1
        )
    assert not (await client.get("/api/v1/admin/maintenance")).json().get("job_id")


async def test_catalog_hides_archive_contents_and_reports_stale_worker(
    client: httpx.AsyncClient,
    backup_environment: dict[str, Any],
) -> None:
    await sign_in(client, "admin", "admin")
    response = await client.get("/api/v1/admin/backups")
    value = response.json()
    assert value["worker_ready"] and len(value["items"]) == 1
    assert "do-not-publish" not in response.text and ".env" not in response.text
    assert value["schedule"] == DEFAULT_SCHEDULE
    await asyncio.to_thread(
        write_signed,
        backup_environment["status"] / "worker.json",
        {"at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
        KEY,
        "heartbeat",
    )
    assert not (await client.get("/api/v1/admin/backups")).json()["worker_ready"]
    await client.post(
        "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Проверка копии"}
    )
    response = await client.post(
        "/api/v1/admin/operations/jobs",
        json={"id": str(uuid4()), "kind": "backup_create", "service": "backup"},
    )
    assert response.status_code == 503
    assert not (await client.get("/api/v1/admin/maintenance")).json().get("job_id")


@pytest.mark.parametrize("login,password", [("student1", "student"), ("teacher", "teacher")])
async def test_backup_api_is_only_available_to_administrator(
    client: httpx.AsyncClient,
    login: str,
    password: str,
) -> None:
    await sign_in(client, login, password)
    assert (await client.get("/api/v1/admin/backups")).status_code == 403
    response = await client.post(
        "/api/v1/admin/operations/jobs",
        json={"id": str(uuid4()), "kind": "backup_create", "service": "backup"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "backup_verify"},
        {"kind": "backup_verify", "snapshot": "../../private"},
        {"kind": "backup_configure"},
        {"kind": "backup_create", "service": "postgres"},
        {"kind": "backup_create", "snapshot": "unexpected"},
        {"kind": "resources", "schedule": DEFAULT_SCHEDULE},
    ],
)
async def test_backup_api_rejects_invalid_operations_before_recording_intent(
    client: httpx.AsyncClient,
    db: AsyncSession,
    backup_environment: dict[str, Any],
    change: dict[str, Any],
) -> None:
    await sign_in(client, "admin", "admin")
    await client.post(
        "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Проверка границ"}
    )
    identity = uuid4()
    response = await client.post(
        "/api/v1/admin/operations/jobs",
        json={
            "id": str(identity),
            "kind": "backup_create",
            "service": "backup",
            **change,
        },
    )
    assert response.status_code == 400
    assert not await db.scalar(select(AuditLog.id).where(AuditLog.entity_id == identity))
    assert not (await client.get("/api/v1/admin/maintenance")).json().get("job_id")
