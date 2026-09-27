"""Container-side bridge used only by the local operator through docker exec."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.db.base import engine, session_factory
from app.db.models import AuditLog, User
from app.domain.maintenance import MAINTENANCE_LOCK, maintenance_state
from app.operations import local_control as local
from app.operations.backup_protocol import write_signed
from app.operations.docker import SERVICES


async def exchange(action: str, value: dict[str, Any]) -> dict[str, Any]:
    if action == "publish":
        stamp = datetime.now(UTC).isoformat()
        services = value["services"]
        interval = services.get("refresh_interval_sec")
        if interval is not None and (type(interval) is not int or not 5 <= interval <= 60):
            raise ValueError("Unexpected monitoring interval")
        if any(item["service"] not in SERVICES for item in services["items"]):
            raise ValueError("Unexpected service")
        services["collected_at"] = stamp
        write_signed(local.ROOT / "services.json", services, local.key(), "technical-host-services")
        for service, logs in value["logs"].items():
            if service not in SERVICES:
                raise ValueError("Unexpected log service")
            write_signed(
                local.ROOT / f"logs-{service}.json",
                {"items": logs, "collected_at": stamp, "refresh_interval_sec": interval},
                local.key(),
                f"technical-host-logs-{service}",
            )
        return {"collected_at": stamp, "services": len(services["items"])}
    async with session_factory() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MAINTENANCE_LOCK})
        request = local.verify_request(value) if action == "begin" else None
        identity = UUID(request["id"] if request else value["id"])
        recorded = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "TECHNICAL_OPERATION_REQUESTED", AuditLog.entity_id == identity
            )
        )
        if not recorded or not recorded.payload:
            raise ValueError("Operation is not recorded by this installation")
        current = local.job(recorded.payload)
        if action == "begin":
            if not await db.scalar(
                select(User.id).where(
                    User.id == recorded.user_id, User.role == "ADMIN", User.is_active.is_(True)
                )
            ):
                raise ValueError("The requesting administrator is no longer active")
            if request != recorded.payload:
                raise ValueError("Request does not match the audit record")
            if current["status"] in {"SUCCEEDED", "FAILED"}:
                return current
            state = await maintenance_state(db)
            if (
                not state["enabled"]
                or state.get("job_id") != str(identity)
                or state.get("update_id")
            ):
                raise ValueError("Maintenance must hold this operation")
            if current["status"] != "QUEUED":
                raise ValueError("Execution already started; inspect it before marking interrupted")
            current.update(status="RUNNING", started_at=datetime.now(UTC).isoformat())
            local.save_job(current)
            db.add(
                AuditLog(
                    user_id=recorded.user_id,
                    action="TECHNICAL_OPERATION_STARTED",
                    entity_type="technical_operation",
                    entity_id=identity,
                    payload={"execution": "LOCAL_COMMAND"},
                )
            )
        elif action == "finish":
            if current["status"] != "RUNNING":
                raise ValueError("Only a running operation can be completed")
            if value.get("status") not in {"SUCCEEDED", "FAILED"}:
                raise ValueError("Invalid operation result")
            current.update(
                status=value["status"],
                result=value.get("result", {}),
                finished_at=datetime.now(UTC).isoformat(),
            )
            if value["status"] == "FAILED":
                current["error"] = "Локальная операция прервана. Проверьте сервис и журнал команды."
            local.save_job(current)
        else:
            raise ValueError("Unknown exchange action")
        await db.commit()
        return current


async def main() -> None:
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("Exchange payload is too large")
        print(json.dumps(await exchange(sys.argv[1], json.loads(raw))))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit(
            "Local exchange refused; check request, maintenance and operation state"
        ) from None
