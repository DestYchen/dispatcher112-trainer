import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.operations.backup_protocol import (
    DEFAULT_SCHEDULE,
    enqueue,
    read_job,
    read_signed,
    scheduled_due,
    validate_request,
    validate_schedule,
    write_signed,
)

KEY = b"test-only-backup-protocol-key-with-sufficient-length"


def request(kind: str = "backup_create") -> dict[str, object]:
    return {"id": str(uuid4()), "actor_id": str(uuid4()), "kind": kind, "service": "backup"}


def test_signed_requests_results_and_retry_do_not_repeat_completed_job(tmp_path: Path) -> None:
    body = request()
    assert enqueue(tmp_path, body, KEY)["status"] == "QUEUED"
    identity = UUID(str(body["id"]))
    result = {"id": str(identity), "status": "SUCCEEDED", "request": body, "result": {"files": 3}}
    write_signed(tmp_path / "results" / f"{identity}.json", result, KEY, "result")
    assert enqueue(tmp_path, body, KEY) == result
    assert read_job(tmp_path, identity, KEY) == result
    with pytest.raises(ValueError, match="already used"):
        enqueue(tmp_path, {**body, "actor_id": str(uuid4())}, KEY)


def test_modified_queue_results_and_wrong_signature_purpose_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    write_signed(path, {"status": "RUNNING"}, KEY, "result")
    with pytest.raises(ValueError, match="signature"):
        read_signed(path, KEY, "request")
    value = json.loads(path.read_text())
    value["value"]["status"] = "SUCCEEDED"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="signature"):
        read_signed(path, KEY, "result")


def test_signed_result_cannot_be_replayed_as_another_job(tmp_path: Path) -> None:
    one, two = request(), request()
    enqueue(tmp_path, one, KEY)
    enqueue(tmp_path, two, KEY)
    write_signed(
        tmp_path / "results" / f"{two['id']}.json",
        {"id": one["id"], "request": one, "status": "SUCCEEDED"},
        KEY,
        "result",
    )
    with pytest.raises(ValueError, match="does not belong"):
        read_job(tmp_path, UUID(str(two["id"])), KEY)


@pytest.mark.parametrize(
    "name", ["../../data", "/etc/passwd", "backup-x", "name;echo hi", "C:\\data"]
)
def test_backup_request_never_accepts_paths_or_commands(name: str) -> None:
    with pytest.raises(ValueError):
        validate_request({**request("backup_verify"), "snapshot": name})


@pytest.mark.parametrize(
    "change",
    [
        {"hour": -1},
        {"hour": 24},
        {"hour": True},
        {"minute": 60},
        {"retention": 13},
        {"retention": 91},
        {"enabled": "true"},
        {"command": "whoami"},
    ],
)
def test_schedule_bounds_and_types_are_strict(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_schedule({**DEFAULT_SCHEDULE, **change})


def test_daily_schedule_is_local_and_success_prevents_duplicate() -> None:
    now = datetime(2026, 9, 26, 3, 30, tzinfo=UTC)
    assert scheduled_due(DEFAULT_SCHEDULE, now, None)
    assert scheduled_due(DEFAULT_SCHEDULE, now, "2026-09-25T21:00:00+00:00")
    assert not scheduled_due(DEFAULT_SCHEDULE, now, "2026-09-26T01:00:00+00:00")
    assert not scheduled_due(DEFAULT_SCHEDULE, now.replace(hour=2), None)
    assert not scheduled_due({**DEFAULT_SCHEDULE, "enabled": False}, now, None)
    moscow = datetime(2026, 9, 26, 3, 0, tzinfo=timezone(timedelta(hours=3)))
    assert not scheduled_due(DEFAULT_SCHEDULE, moscow, "2026-09-25T23:00:00+00:00")


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 31])
def test_missing_or_short_control_key_cannot_authorize_requests(tmp_path: Path, key: bytes) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        enqueue(tmp_path, request(), key)
