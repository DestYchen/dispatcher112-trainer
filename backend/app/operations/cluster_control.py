"""Validate local backend scaling against the configured PostgreSQL connection budget."""

import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db.base import engine, session_factory
from app.db.models import AuditLog
from app.domain.runtime_configuration import read_snapshot
from app.operations.update_database import lock_and_authorize, save_setting, setting


async def command(value: dict[str, Any]) -> dict[str, Any]:
    actor, identity = UUID(value["actor"]), UUID(value["id"])
    replicas = value["replicas"]
    if type(replicas) is not int or not 1 <= replicas <= 4:
        raise ValueError("Use one to four backend instances")
    async with session_factory() as db:
        await lock_and_authorize(db, actor)
        maintenance = await setting(db, "maintenance")
        current = await setting(db, "runtime_topology")
        if value["action"] == "status":
            return current
        if (
            value["action"] in {"prepare", "finish"}
            and current.get("id") == str(identity)
            and current.get("phase") == "APPLIED"
        ):
            if current["backend_replicas"] != replicas:
                raise ValueError("Scaling request parameters changed")
            # The host may have stopped after the commit but before saving its journal.
            return current
        if (
            not maintenance.get("enabled")
            or any(maintenance.get(field) for field in ("job_id", "update_id", "switch_id"))
            or maintenance.get("topology_id") not in {None, str(identity)}
        ):
            raise ValueError("Scaling requires exclusive maintenance")
        if await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM lessons WHERE status='RUNNING') OR "
                "EXISTS(SELECT 1 FROM generation_jobs WHERE status IN ('QUEUED','RUNNING')) OR "
                "EXISTS(SELECT 1 FROM sip_calls WHERE state IN ('REQUESTED','RINGING','CONNECTED'))"
            )
        ):
            raise ValueError("Finish active work before scaling")
        snapshot = await read_snapshot(db)
        params = snapshot.configuration.database
        required = params.connection_limit() + (replicas - 1) * (
            params.api_pool_size + params.max_overflow
        )
        available = int(
            await db.scalar(
                text(
                    "SELECT current_setting('max_connections')::int "
                    "- current_setting('superuser_reserved_connections')::int - 10"
                )
            )
            or 0
        )
        if required > available:
            raise ValueError("Reduce the database pools before adding backend instances")
        if value["action"] == "prepare":
            if current.get("id") == str(identity) and current.get("phase") == "APPLYING":
                if current["backend_replicas"] != replicas:
                    raise ValueError("Scaling request parameters changed")
                return current
            current = {
                "id": str(identity),
                "backend_replicas": replicas,
                "phase": "APPLYING",
                "connection_limit": required,
                "connection_budget": available,
            }
            await save_setting(
                db, "maintenance", {**maintenance, "topology_id": str(identity)}, actor
            )
        elif value["action"] == "finish":
            if current.get("id") != str(identity) or current.get("backend_replicas") != replicas:
                raise ValueError("Unknown scaling request")
            current = {**current, "phase": "APPLIED"}
            await save_setting(
                db,
                "maintenance",
                {key: item for key, item in maintenance.items() if key != "topology_id"},
                actor,
            )
        else:
            raise ValueError("Unsupported scaling action")
        await save_setting(db, "runtime_topology", current, actor)
        db.add(
            AuditLog(
                user_id=actor,
                action="RUNTIME_TOPOLOGY_" + current["phase"],
                entity_type="system_settings",
                entity_id=identity,
                payload=current,
            )
        )
        await db.commit()
        return current


async def main() -> None:
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError("Scaling request is too large")
        print(json.dumps(await command(json.loads(raw))))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
