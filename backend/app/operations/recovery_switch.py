"""Local installation operator: fence changes and preserve audit across recovery routing."""

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.db.base import engine, session_factory
from app.db.models import AuditLog
from app.operations.snapshot import canonical
from app.operations.update_database import (
    MAX_TAIL_BYTES,
    MAX_TAIL_ROWS,
    audit_row,
    lock_and_authorize,
    save_setting,
    setting,
)


async def command(value: dict[str, Any]) -> dict[str, Any]:
    actor, identity = UUID(value["actor"]), UUID(value["id"])
    action = value["action"]
    async with session_factory() as db:
        await lock_and_authorize(db, actor)
        maintenance = await setting(db, "maintenance")
        current = await setting(db, "recovery_switch")
        if action == "fence":
            if current.get("id") == str(identity) and current.get("phase") == "FENCED":
                return current
            if any(
                maintenance.get(field)
                for field in ("job_id", "update_id", "switch_id", "topology_id")
            ):
                raise ValueError("Another maintenance operation is active")
            if await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM lessons WHERE status='RUNNING') "
                    "OR EXISTS(SELECT 1 FROM generation_jobs WHERE status IN ('QUEUED','RUNNING')) "
                    "OR EXISTS(SELECT 1 FROM sip_calls "
                    "WHERE state IN ('REQUESTED','RINGING','CONNECTED'))"
                )
            ):
                raise ValueError("Finish lessons, generation and calls before switching")
            reason = value["reason"].strip()
            if not 5 <= len(reason) <= 1000:
                raise ValueError("A switching reason is required")
            current = {
                "id": str(identity),
                "phase": "FENCED",
                "reason": reason,
                "previous_maintenance": maintenance,
                "at": datetime.now(UTC).isoformat(),
            }
            await save_setting(
                db,
                "maintenance",
                {"enabled": True, "reason": reason, "switch_id": str(identity)},
                actor,
            )
        elif action in {"export", "import", "release"}:
            if (
                current.get("id") == str(identity)
                and action == "release"
                and current.get("phase") == value["phase"]
            ):
                return current
            if current.get("id") != str(identity) or maintenance.get("switch_id") != str(identity):
                raise ValueError("Switching operation does not own maintenance")
            if action == "export":
                stream = await db.stream_scalars(
                    text("SELECT to_jsonb(a) FROM audit_log a ORDER BY id"),
                    execution_options={"yield_per": 1000},
                )
                rows: list[dict[str, Any]] = []
                size = 0
                async for raw in stream:
                    row = audit_row(raw)
                    size += len(canonical(row))
                    if len(rows) >= MAX_TAIL_ROWS or size > MAX_TAIL_BYTES:
                        raise ValueError(
                            "Audit transfer limit exceeded; source database is preserved"
                        )
                    rows.append(row)
                return {"source": value["source"], "rows": rows}
            if action == "import":
                incoming = value["history"]
                if not isinstance(incoming["rows"], list) or len(incoming["rows"]) > MAX_TAIL_ROWS:
                    raise ValueError("Invalid audit history")
                known = set(
                    await db.scalars(
                        select(AuditLog.payload["sha256"].astext).where(
                            AuditLog.action == "RECOVERY_AUDIT_IMPORTED"
                        )
                    )
                )
                existing = {
                    row[0]: row[1]
                    for row in await db.execute(text("SELECT id,to_jsonb(a) FROM audit_log a"))
                }
                imported = 0
                for raw in incoming["rows"]:
                    row, source = audit_row(raw), incoming["source"]
                    if row["action"] == "RECOVERY_AUDIT_IMPORTED":
                        source = row["payload"]["source"]
                        row = audit_row(row["payload"]["original"])
                    digest = hashlib.sha256(canonical(row)).hexdigest()
                    if digest in known or (
                        row["id"] in existing
                        and canonical(audit_row(existing[row["id"]])) == canonical(row)
                    ):
                        continue
                    db.add(
                        AuditLog(
                            user_id=actor,
                            action="RECOVERY_AUDIT_IMPORTED",
                            entity_type="recovery_history",
                            entity_id=identity,
                            payload={
                                "source": source,
                                "original_id": row["id"],
                                "sha256": digest,
                                "original": row,
                            },
                        )
                    )
                    known.add(digest)
                    imported += 1
                current = {**current, "imported": current.get("imported", 0) + imported}
            else:
                phase = value["phase"]
                if phase not in {"ACTIVE", "RETURNED", "STANDBY"}:
                    raise ValueError("Invalid switching phase")
                await save_setting(db, "maintenance", current["previous_maintenance"], actor)
                current = {**current, "phase": phase, "at": datetime.now(UTC).isoformat()}
        else:
            raise ValueError("Unsupported recovery switch action")
        await save_setting(db, "recovery_switch", current, actor)
        db.add(
            AuditLog(
                user_id=actor,
                action="RECOVERY_SWITCH_" + action.upper(),
                entity_type="recovery_switch",
                entity_id=identity,
                payload={
                    key: item for key, item in current.items() if key != "previous_maintenance"
                },
            )
        )
        await db.commit()
        return current


async def main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_TAIL_BYTES + 1024 * 1024 + 1)
        if len(raw) > MAX_TAIL_BYTES + 1024 * 1024:
            raise ValueError("Recovery exchange is too large")
        print(json.dumps(await command(json.loads(raw))))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit(
            "Recovery switch refused; check active work, administrator and operation state"
        ) from None
