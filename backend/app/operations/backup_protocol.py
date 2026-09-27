"""Signed local backup requests and results; never accepts shell commands or paths."""

import hashlib
import hmac
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

DEFAULT_SCHEDULE = {"enabled": True, "hour": 3, "minute": 0, "retention": 14}
BACKUP_KINDS = frozenset(
    {
        "backup_create",
        "backup_verify",
        "backup_configure",
        "backup_integrity_check",
        "backup_integrity_baseline",
    }
)


def validate_schedule(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != set(DEFAULT_SCHEDULE) or type(value["enabled"]) is not bool:
        raise ValueError("Invalid backup schedule")
    for field, minimum, maximum in (("hour", 0, 23), ("minute", 0, 59), ("retention", 14, 90)):
        if type(value[field]) is not int or not minimum <= value[field] <= maximum:
            raise ValueError("Invalid backup schedule")
    return dict(value)


def validate_request(value: dict[str, Any]) -> None:
    if str(UUID(value["id"])) != value["id"] or str(UUID(value["actor_id"])) != value["actor_id"]:
        raise ValueError("Invalid operation identity")
    if value["kind"] not in BACKUP_KINDS or value["service"] != "backup":
        raise ValueError("Unsupported backup operation")
    if value["kind"] in {"backup_verify", "backup_integrity_baseline"}:
        if not isinstance(value.get("snapshot"), str) or not re.fullmatch(
            r"backup-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", value["snapshot"]
        ):
            raise ValueError("Invalid snapshot identifier")
    elif value.get("snapshot") is not None:
        raise ValueError("Unexpected snapshot identifier")
    if value["kind"] == "backup_configure":
        if not isinstance(value.get("schedule"), dict):
            raise ValueError("Missing backup schedule")
        validate_schedule(value["schedule"])
    elif value.get("schedule") is not None:
        raise ValueError("Unexpected backup schedule")
    if value["kind"] == "backup_integrity_baseline":
        if (
            not isinstance(value.get("reason"), str)
            or not 5 <= len(value["reason"].strip()) <= 1000
        ):
            raise ValueError("A baseline approval reason is required")
    elif value.get("reason") is not None:
        raise ValueError("Unexpected baseline reason")


def signature(value: dict[str, Any], key: bytes, purpose: str) -> str:
    if len(key) < 32:
        raise ValueError("Backup control key must contain at least 32 bytes")
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key, purpose.encode() + b"\x00" + data, hashlib.sha256).hexdigest()


def write_signed(path: Path, value: dict[str, Any], key: bytes, purpose: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Symbolic links are not permitted")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            group = path.parent.stat().st_gid
            if os.fstat(stream.fileno()).st_gid != group:
                os.fchown(stream.fileno(), -1, group)
            os.fchmod(stream.fileno(), 0o640)
            json.dump({"value": value, "signature": signature(value, key, purpose)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_signed(path: Path, key: bytes, purpose: str) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 262144:
        raise ValueError("Invalid operation file")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    value = envelope["value"]
    if not isinstance(value, dict) or not hmac.compare_digest(
        envelope["signature"], signature(value, key, purpose)
    ):
        raise ValueError("Invalid operation signature")
    return value


def enqueue(root: Path, request: dict[str, Any], key: bytes) -> dict[str, Any]:
    validate_request(request)
    identity = UUID(request["id"])
    path = root / "requests" / f"{identity}.json"
    if path.exists():
        if read_signed(path, key, "request") != request:
            raise ValueError("Operation identifier already used")
    else:
        write_signed(path, request, key, "request")
    return read_job(root, identity, key)


def read_job(root: Path, identity: UUID, key: bytes) -> dict[str, Any]:
    request = read_signed(root / "requests" / f"{identity}.json", key, "request")
    validate_request(request)
    path = root / "results" / f"{identity}.json"
    if path.exists():
        result = read_signed(path, key, "result")
        if result.get("id") != str(identity) or result.get("request") != request:
            raise ValueError("Result does not belong to this request")
        if result.get("status") not in {"RUNNING", "SUCCEEDED", "FAILED"}:
            raise ValueError("Invalid job status")
        return result
    return {"id": str(identity), "status": "QUEUED", "request": request}


def scheduled_due(schedule: dict[str, Any], now: datetime, last_backup_at: str | None) -> bool:
    validate_schedule(schedule)
    if not schedule["enabled"] or (now.hour, now.minute) < (schedule["hour"], schedule["minute"]):
        return False
    return (
        not last_backup_at
        or datetime.fromisoformat(last_backup_at).astimezone(now.tzinfo).date() < now.date()
    )
