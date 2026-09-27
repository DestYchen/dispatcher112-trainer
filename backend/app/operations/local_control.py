"""Signed exchanges with an explicitly invoked local operator; no Docker access."""

import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import settings
from app.operations.backup_protocol import read_signed, signature, write_signed
from app.operations.docker import MIN_MEMORY, RESTART_ONLY, SERVICES
from app.operations.update_catalog import TOKEN, request_key

ROOT = Path("/data/materials/.technical-host")
PURPOSE = "technical-host-request-1"


def key() -> bytes:
    return request_key(TOKEN.read_bytes().strip(), settings.jwt_secret)


def validate(value: dict[str, Any]) -> None:
    for field in ("id", "actor_id"):
        if str(UUID(value[field])) != value[field]:
            raise ValueError("Invalid operation identity")
    service = value["service"]
    if service not in SERVICES or value["kind"] not in {"service", "resources"}:
        raise ValueError("Unsupported local operation")
    if value["kind"] == "service":
        action = value["action"]
        if action not in {"start", "stop", "restart"} or (
            service in RESTART_ONLY and action != "restart"
        ):
            raise ValueError("Unsupported service action")
    elif (
        type(value["cpus"]) not in {int, float}
        or not 0.5 <= value["cpus"] <= 64
        or type(value["memory_mb"]) is not int
        or not MIN_MEMORY.get(service, 128) <= value["memory_mb"] <= 65536
    ):
        raise ValueError("Invalid resource limits")


def save_job(value: dict[str, Any]) -> None:
    identity = UUID(value["id"])
    write_signed(ROOT / "jobs" / f"{identity}.json", value, key(), "technical-host-result")


def job(request: dict[str, Any]) -> dict[str, Any]:
    validate(request)
    path = ROOT / "jobs" / f"{UUID(request['id'])}.json"
    if path.exists():
        result = read_signed(path, key(), "technical-host-result")
        if result.get("request") != request:
            raise ValueError("Operation identifier already used")
        return result
    result = {
        "id": request["id"],
        "request": request,
        "status": "QUEUED",
        "execution": "LOCAL_COMMAND",
        "created_at": datetime.now(UTC).isoformat(),
    }
    # The database maintenance lock serializes creation of the request.
    save_job(result)
    return result


def export_request(request: dict[str, Any]) -> dict[str, Any]:
    current = job(request)
    if current["status"] != "QUEUED":
        raise ValueError("Only a queued operation can be exported")
    now = datetime.now(UTC)
    value = {
        "request": request,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }
    return {"value": value, "signature": signature(value, key(), PURPOSE)}


def verify_request(envelope: dict[str, Any]) -> dict[str, Any]:
    value = envelope["value"]
    if not hmac.compare_digest(envelope["signature"], signature(value, key(), PURPOSE)):
        raise ValueError("Request signature belongs to another installation or is invalid")
    created, expires = (
        datetime.fromisoformat(value[name]) for name in ("created_at", "expires_at")
    )
    now = datetime.now(UTC)
    if (
        created.tzinfo is None
        or expires.tzinfo is None
        or expires - created != timedelta(hours=24)
        or created > now + timedelta(seconds=60)
        or expires <= now
    ):
        raise ValueError("Local request has expired")
    request: dict[str, Any] = value["request"]
    validate(request)
    return request


def report(service: str | None = None) -> dict[str, Any]:
    name = f"logs-{service}" if service else "services"
    path = ROOT / f"{name}.json"
    if not path.exists():
        return {"items": [], "execution": "LOCAL_COMMAND", "collected_at": None, "stale": True}
    result = read_signed(path, key(), f"technical-host-{name}")
    age = (datetime.now(UTC) - datetime.fromisoformat(result["collected_at"])).total_seconds()
    interval = result.get("refresh_interval_sec")
    lifetime = max(15, interval * 3) if type(interval) is int and 5 <= interval <= 60 else 300
    return {**result, "execution": "LOCAL_COMMAND", "stale": not 0 <= age <= lifetime}
