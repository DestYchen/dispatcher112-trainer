import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.operations import backup_client, backup_worker
from app.operations import file_integrity as integrity
from app.operations.backup_protocol import write_signed
from app.operations.snapshot import create_snapshot, prune_snapshots, read_manifest
from tests.test_auth import sign_in
from tests.test_program_snapshots import program_fixture

KEY = b"test-integrity-signing-key-only-000000000000"


@pytest.fixture
def installation(tmp_path: Path) -> dict[str, Any]:
    roots = {
        "program": program_fixture(tmp_path / "program"),
        "config": tmp_path / "config",
        "data": tmp_path / "data",
    }
    for scope in ("config", "data"):
        roots[scope].mkdir()
    (roots["config"] / ".env").write_text("SECRET=private-installation-value")
    (roots["config"] / "docker-compose.yml").write_text("services: {}")
    (roots["config"] / ".secrets").mkdir()
    (roots["config"] / ".secrets/tls.key").write_text("private-test-key")
    (roots["data"] / "voices").mkdir()
    (roots["data"] / "voices/greeting.wav").write_bytes(b"RIFF-test-offline-resource")
    (roots["data"] / "materials").mkdir()
    (roots["data"] / "materials/lesson.txt").write_text("Mutable learning material")
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"test-only-dump")
    backups = tmp_path / "backups"
    snapshot = create_snapshot(
        backups, roots["data"], roots["config"], dump, KEY, program_root=roots["program"]
    )
    request = {
        "snapshot": snapshot["name"],
        "id": str(uuid4()),
        "actor_id": str(uuid4()),
        "reason": "Accept a verified full installation",
    }
    return {"roots": roots, "backups": backups, "request": request, "dump": dump}


def approve(value: dict[str, Any]) -> dict[str, Any]:
    return integrity.select_baseline(value["backups"], KEY, value["request"], value["roots"])


def test_no_baseline_is_not_a_success_and_approval_is_explicit(
    installation: dict[str, Any],
) -> None:
    value = installation
    assert integrity.check(value["backups"], KEY, value["roots"])["status"] == "EMPTY"
    selected = approve(value)
    repeated = approve(value)
    assert selected["baseline"] == repeated["baseline"]
    checked = integrity.check(value["backups"], KEY, value["roots"])
    assert checked["status"] == "OK" and checked["files"] == checked["expected_files"]
    assert checked["baseline"]["actor_id"] == value["request"]["actor_id"]
    assert checked["scopes"] == ["program", "config", "static_resources"]
    assert "private-installation-value" not in json.dumps(checked)
    assert "private-test-key" not in json.dumps(checked)


@pytest.mark.parametrize(
    "scope,path,change,kind",
    [
        ("program", "backend/app/main.py", "replace", "CHANGED"),
        ("program", "frontend/src/main.tsx", "remove", "MISSING"),
        ("program", "frontend/src/injected.ts", "extra", "EXTRA"),
        ("config", ".env", "replace", "CHANGED"),
        ("config", ".secrets/tls.key", "remove", "MISSING"),
        ("config", "infra/new.conf", "extra", "EXTRA"),
        ("data", "voices/greeting.wav", "replace", "CHANGED"),
        ("data", "voices/new.wav", "extra", "EXTRA"),
    ],
)
def test_each_scope_detects_changed_missing_and_added_files(
    installation: dict[str, Any],
    scope: str,
    path: str,
    change: str,
    kind: str,
) -> None:
    value = installation
    approve(value)
    target = value["roots"][scope] / path
    if change == "remove":
        target.unlink()
    else:
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_bytes(b"modified contents")
    result = integrity.check(value["backups"], KEY, value["roots"])
    assert result["status"] == "FAIL"
    assert {"scope": scope, "path": path, "kind": kind} in result["issues"]
    selected = integrity.baseline(value["backups"], KEY)
    assert selected is not None and selected["id"] == value["request"]["id"]
    with pytest.raises(ValueError, match="do not match"):
        approve(value)


def test_mutable_teaching_files_and_caches_do_not_invalidate_static_scope(
    installation: dict[str, Any],
) -> None:
    value = installation
    approve(value)
    (value["roots"]["data"] / "materials/new.pdf").write_bytes(b"New legitimate teaching material")
    cache = value["roots"]["program"] / "frontend/node_modules/test"
    cache.mkdir(parents=True)
    (cache / "cache.bin").write_bytes(b"not in the source baseline")
    assert integrity.check(value["backups"], KEY, value["roots"])["status"] == "OK"


@pytest.mark.parametrize("target", ["marker", "manifest"])
def test_tampering_never_returns_ok(installation: dict[str, Any], target: str) -> None:
    value = installation
    approve(value)
    path = (
        value["backups"] / integrity.BASELINE
        if target == "marker"
        else value["backups"] / "snapshots" / value["request"]["snapshot"] / "manifest.json"
    )
    path.write_text('{"value":{},"signature":"forged"}')
    assert integrity.check(value["backups"], KEY, value["roots"])["status"] == "FAIL"


def test_source_links_cannot_read_outside_roots(
    installation: dict[str, Any], tmp_path: Path
) -> None:
    value = installation
    approve(value)
    external = tmp_path / "outside.key"
    external.write_bytes(b"private-outside-value")
    path = value["roots"]["config"] / ".env"
    path.unlink()
    path.symlink_to(external)
    result = integrity.check(value["backups"], KEY, value["roots"])
    assert result["status"] == "FAIL"
    assert {"scope": "config", "path": ".env", "kind": "INVALID"} in result["issues"]
    assert "private-outside-value" not in json.dumps(result)


def test_report_limits_preserve_total_and_never_hide_failure(installation: dict[str, Any]) -> None:
    value = installation
    approve(value)
    for number in range(integrity.MAX_ISSUES + 4):
        (value["roots"]["data"] / f"voices/unexpected-{number}.wav").write_bytes(b"extra")
    result = integrity.check(value["backups"], KEY, value["roots"])
    assert result["status"] == "FAIL" and result["truncated"]
    assert len(result["issues"]) == integrity.MAX_ISSUES
    assert result["issues_total"] == integrity.MAX_ISSUES + 4


def test_permission_failure_is_unavailable_not_ok(
    installation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = installation
    approve(value)

    def denied(*args: Any) -> tuple[int, str]:
        raise PermissionError("private diagnostic text")

    monkeypatch.setattr(integrity, "digest", denied)
    result = integrity.check(value["backups"], KEY, value["roots"])
    assert result["status"] == "UNAVAILABLE"
    assert "private diagnostic text" not in json.dumps(result)


def test_old_snapshots_cannot_become_baseline(installation: dict[str, Any]) -> None:
    value = installation
    snapshot = create_snapshot(
        value["backups"], value["roots"]["data"], value["roots"]["config"], value["dump"], KEY
    )
    value["request"]["snapshot"] = snapshot["name"]
    with pytest.raises(ValueError, match="including the program"):
        approve(value)
    assert integrity.baseline(value["backups"], KEY) is None


def test_pruning_pins_the_approved_snapshot_and_its_objects(installation: dict[str, Any]) -> None:
    value = installation
    approve(value)
    oldest = value["request"]["snapshot"]
    for number in range(15):
        (value["roots"]["data"] / "voices/greeting.wav").write_text(str(number))
        create_snapshot(
            value["backups"],
            value["roots"]["data"],
            value["roots"]["config"],
            value["dump"],
            KEY,
            program_root=value["roots"]["program"],
        )
    assert len(list((value["backups"] / "snapshots").iterdir())) == 15
    manifest = read_manifest(value["backups"], oldest, KEY)
    assert all(
        (value["backups"] / "objects" / row["sha256"]).is_file() for row in manifest["files"]
    )
    (value["backups"] / integrity.BASELINE).write_text("broken marker")
    before = sorted(path.name for path in (value["backups"] / "snapshots").iterdir())
    with pytest.raises(ValueError):
        prune_snapshots(value["backups"], KEY, 14)
    assert before == sorted(path.name for path in (value["backups"] / "snapshots").iterdir())


@pytest.fixture
def executor(installation: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    value = installation
    parent = value["backups"].parent
    token = parent / "control-key"
    signing = parent / "signing-key"
    token.write_bytes(KEY)
    signing.write_bytes(KEY)
    for module in (backup_client, backup_worker):
        monkeypatch.setattr(module, "ROOT", parent / "queue")
        monkeypatch.setattr(module, "STATUS", parent / "status")
        monkeypatch.setattr(module, "TOKEN", token)
    monkeypatch.setattr(backup_worker, "BACKUPS", value["backups"])
    monkeypatch.setattr(backup_worker, "SIGNING_KEY", signing)
    monkeypatch.setattr(backup_worker, "INTEGRITY_ROOTS", value["roots"])
    write_signed(
        parent / "status/worker.json", {"at": datetime.now(UTC).isoformat()}, KEY, "heartbeat"
    )
    backup_worker.publish_catalog(KEY)
    return value


async def test_admin_approves_a_verified_baseline_with_audit_and_can_check_after_damage(
    executor: dict[str, Any],
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    await sign_in(client, "admin", "admin")
    status = await client.get("/api/v1/admin/diagnostics/files")
    assert status.status_code == 503
    backup_worker.check_files(KEY)
    assert (await client.get("/api/v1/admin/diagnostics/files")).json()["status"] == "EMPTY"
    body = {key: value for key, value in executor["request"].items() if key != "actor_id"}
    body.update(kind="backup_integrity_baseline", service="backup")
    operation = "/api/v1/admin/operations/jobs"
    assert (await client.post(operation, json=body)).status_code == 409
    assert (
        await client.post(
            "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Проверка эталона"}
        )
    ).status_code == 200
    rejected = await client.post(operation, json=body)
    assert (
        rejected.status_code == 409 and rejected.json()["error"]["code"] == "BASELINE_NOT_VERIFIED"
    )
    verification = {"snapshot": body["snapshot"], "verified_at": datetime.now(UTC).isoformat()}
    write_signed(
        executor["backups"] / "verifications" / f"{body['snapshot']}.json",
        verification,
        KEY,
        "verification",
    )
    backup_worker.publish_catalog(KEY)
    assert (
        await client.post(operation, json=body, headers={"X-CSRF-Token": "invalid"})
    ).status_code == 403
    assert (await client.post(operation, json={**body, "reason": "  "})).status_code == 400
    assert (await client.post(operation, json=body)).status_code == 202
    assert backup_worker.process_one(KEY)
    result = (await client.get(operation + "/" + str(body["id"]))).json()
    assert result["status"] == "SUCCEEDED" and result["result"]["status"] == "OK"
    selected = result["result"]["baseline"]
    replay = await client.post(operation, json=body)
    assert replay.status_code == 202 and replay.json()["result"]["baseline"] == selected
    assert not backup_worker.process_one(KEY)
    assert (await client.get(operation + "/" + str(body["id"]))).status_code == 200
    finished = list(
        await db.scalars(select(AuditLog).where(AuditLog.action == "TECHNICAL_OPERATION_FINISHED"))
    )
    assert len(finished) == 1
    assert (
        finished[0].payload is not None
        and finished[0].payload["request"]["reason"] == body["reason"]
    )
    source = executor["roots"]["program"] / "backend/app/main.py"
    source.write_text("changed after approval")
    check = {"id": str(uuid4()), "kind": "backup_integrity_check", "service": "backup"}
    assert (await client.post(operation, json=check)).status_code == 202
    assert backup_worker.process_one(KEY)
    result = (await client.get(operation + "/" + check["id"])).json()
    assert result["status"] == "SUCCEEDED" and result["result"]["status"] == "FAIL"
    current = await client.get("/api/v1/admin/diagnostics/files")
    assert current.status_code == 200 and current.json()["status"] == "FAIL"
    assert current.headers["Cache-Control"] == "private, no-store"
    assert current.json()["baseline"] == selected


@pytest.mark.parametrize("user,password", [("teacher", "teacher"), ("student1", "student")])
async def test_teaching_roles_cannot_read_or_approve_integrity(
    executor: dict[str, Any],
    client: AsyncClient,
    user: str,
    password: str,
) -> None:
    await sign_in(client, user, password)
    assert (await client.get("/api/v1/admin/diagnostics/files")).status_code == 403
    body = {key: value for key, value in executor["request"].items() if key != "actor_id"}
    body.update(kind="backup_integrity_baseline", service="backup")
    assert (await client.post("/api/v1/admin/operations/jobs", json=body)).status_code == 403


@pytest.mark.parametrize(
    "mode", ["worker_expired", "result_expired", "future", "forged", "running_expired"]
)
async def test_old_or_invalid_reports_never_show_success(
    executor: dict[str, Any], mode: str
) -> None:
    from datetime import timedelta

    from app.api.errors import APIError

    approve(executor)
    backup_worker.check_files(KEY)
    assert (await backup_client.file_integrity())["status"] == "OK"
    now = datetime.now(UTC)
    if mode in {"worker_expired", "future"}:
        moment = (
            now - timedelta(seconds=31) if mode == "worker_expired" else now + timedelta(seconds=30)
        )
        write_signed(
            backup_client.STATUS / "worker.json", {"at": moment.isoformat()}, KEY, "heartbeat"
        )
    elif mode == "forged":
        (backup_client.STATUS / "file-integrity.json").write_text("{}")
    else:
        value = {
            "status": "RUNNING" if mode == "running_expired" else "OK",
            "started_at": (now - timedelta(seconds=181)).isoformat(),
            "checked_at": (now - timedelta(seconds=601)).isoformat(),
        }
        write_signed(backup_client.STATUS / "file-integrity.json", value, KEY, "file-integrity")
    if mode in {"worker_expired", "future", "forged"}:
        with pytest.raises(APIError) as failure:
            await backup_client.file_integrity()
        assert failure.value.code == "INTEGRITY_UNAVAILABLE"
    else:
        assert (await backup_client.file_integrity())["status"] == "UNAVAILABLE"
