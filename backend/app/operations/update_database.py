"""Transactional update ownership and append-only audit preservation for the host installer."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.domain.maintenance import MAINTENANCE_LOCK
from app.operations.snapshot import SNAPSHOT_NAME, canonical
from app.operations.update_files import read_record, write_record

TRANSITIONS = {
    "STARTED": {"BACKED_UP", "ROLLING_BACK", "FAILED"},
    "BACKED_UP": {"MIGRATED", "ROLLING_BACK", "FAILED"},
    "MIGRATED": {"READY", "ROLLING_BACK", "FAILED"},
    "READY": {"ACTIVE", "ROLLING_BACK", "FAILED"},
    "FAILED": {"ROLLING_BACK"},
    "ROLLING_BACK": {"ROLLED_BACK", "FAILED"},
    "ACTIVE": set(),
    "ROLLED_BACK": set(),
}
TERMINAL = {"ACTIVE", "ROLLED_BACK"}
AUDIT_FIELDS = {
    "id",
    "user_id",
    "action",
    "entity_type",
    "entity_id",
    "payload",
    "ip",
    "created_at",
}
MAX_TAIL_ROWS = 100_000
MAX_TAIL_BYTES = 64 * 1024 * 1024


async def lock_and_authorize(db: AsyncSession, actor: UUID) -> None:
    if not await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MAINTENANCE_LOCK}
    ):
        raise ValueError("An application transaction or maintenance operation is in progress")
    if not await db.scalar(
        text("SELECT id FROM users WHERE id=:actor AND role='ADMIN' AND is_active"),
        {"actor": actor},
    ):
        raise ValueError("An active administrator must own the software operation")


async def setting(db: AsyncSession, key: str) -> dict[str, Any]:
    value = await db.scalar(text("SELECT value FROM system_settings WHERE key=:key"), {"key": key})
    return dict(value) if value else {}


async def save_setting(db: AsyncSession, key: str, value: dict[str, Any], actor: UUID) -> None:
    await db.execute(
        text(
            "INSERT INTO system_settings (key,value,updated_by) "
            "VALUES (:key,CAST(:value AS jsonb),:actor) ON CONFLICT (key) DO UPDATE "
            "SET value=EXCLUDED.value,updated_by=EXCLUDED.updated_by,updated_at=now()"
        ),
        {"key": key, "value": json.dumps(value), "actor": actor},
    )


async def record(db: AsyncSession, action: str, value: dict[str, Any], actor: UUID) -> None:
    await db.execute(
        text(
            "INSERT INTO audit_log (user_id,action,entity_type,entity_id,payload) "
            "VALUES (:actor,:action,'software_update',"
            "CAST(:identity AS uuid),CAST(:value AS jsonb))"
        ),
        {"actor": actor, "action": action, "identity": value["id"], "value": json.dumps(value)},
    )


def audit_row(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != AUDIT_FIELDS:
        raise ValueError("Invalid audit row fields")
    if type(value["id"]) is not int or not 0 < value["id"] < 2**63:
        raise ValueError("Invalid audit identity")
    result = dict(value)
    for key in ("user_id", "entity_id"):
        if result[key] is not None:
            if not isinstance(result[key], str):
                raise ValueError("Audit actor and entity identities must be UUID strings")
            result[key] = str(UUID(result[key]))
    for key in ("action", "entity_type"):
        if result[key] is None and key == "entity_type":
            continue
        if not isinstance(result[key], str) or not 1 <= len(result[key]) <= 64:
            raise ValueError("Invalid audit action or entity")
    if result["payload"] is not None and not isinstance(result["payload"], dict):
        raise ValueError("Invalid audit payload")
    if result["ip"] is not None:
        if not isinstance(result["ip"], str):
            raise ValueError("Audit IP addresses must be strings")
        result["ip"] = str(ipaddress.ip_interface(result["ip"]))
    if not isinstance(result["created_at"], str):
        raise ValueError("Invalid audit timestamp")
    created = datetime.fromisoformat(result["created_at"])
    if created.tzinfo is None:
        raise ValueError("Audit timestamps must include a timezone")
    result["created_at"] = created.astimezone(UTC).isoformat()
    return result


async def audit_anchor(db: AsyncSession, maximum: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    rows = await db.stream_scalars(
        text("SELECT to_jsonb(a) FROM audit_log a WHERE id<=:maximum ORDER BY id"),
        {"maximum": maximum},
        execution_options={"yield_per": 1000},
    )
    async for row in rows:
        digest.update(canonical(audit_row(row)) + b"\n")
        count += 1
    return {"maximum": maximum, "count": count, "sha256": digest.hexdigest()}


async def begin_update(
    db: AsyncSession, identity: UUID, actor: UUID, version: str, package_sha256: str, reason: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("Invalid software version")
    if not re.fullmatch(r"[a-f0-9]{64}", package_sha256) or not 5 <= len(reason.strip()) <= 1000:
        raise ValueError("A package digest and operation reason are required")
    await lock_and_authorize(db, actor)
    request = {
        "id": str(identity),
        "actor_id": str(actor),
        "version": version,
        "package_sha256": package_sha256,
        "reason": reason.strip(),
    }
    current = await setting(db, "software_update")
    if current.get("id") == str(identity):
        if any(current.get(key) != value for key, value in request.items()):
            raise ValueError("Update identity belongs to a different request")
        return current
    if current and current.get("phase") not in TERMINAL:
        raise ValueError("Another software update is incomplete")
    if await db.scalar(
        text("SELECT 1 FROM audit_log WHERE action='SOFTWARE_UPDATE_STARTED' AND entity_id=:id"),
        {"id": identity},
    ):
        raise ValueError("A completed update identity cannot be reused")
    maintenance = await setting(db, "maintenance") or {"enabled": False, "reason": ""}
    recovery = await setting(db, "recovery")
    if (
        maintenance.get("job_id")
        or maintenance.get("switch_id")
        or maintenance.get("topology_id")
        or maintenance.get("update_id")
        or recovery.get("status") == "PREPARED"
    ):
        raise ValueError("Another maintenance or recovery operation is incomplete")
    busy = await db.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM lessons WHERE status='RUNNING') OR "
            "EXISTS(SELECT 1 FROM generation_jobs WHERE status IN ('QUEUED','RUNNING')) OR "
            "EXISTS(SELECT 1 FROM sip_calls WHERE state IN ('REQUESTED','RINGING','CONNECTED'))"
        )
    )
    if busy:
        raise ValueError("Finish lessons, generation jobs and calls before updating")
    maximum = int(await db.scalar(text("SELECT coalesce(max(id),0) FROM audit_log")) or 0)
    value = {
        **request,
        "phase": "STARTED",
        "started_at": datetime.now(UTC).isoformat(),
        "previous_maintenance": maintenance,
        "audit_anchor": await audit_anchor(db, maximum),
    }
    await save_setting(db, "software_update", value, actor)
    await save_setting(
        db,
        "maintenance",
        {
            "enabled": True,
            "reason": reason.strip(),
            "update_id": str(identity),
        },
        actor,
    )
    await record(db, "SOFTWARE_UPDATE_STARTED", value, actor)
    return value


async def advance_update(
    db: AsyncSession, identity: UUID, actor: UUID, phase: str, details: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(details, dict):
        raise ValueError("Software phase details must be an object")
    await lock_and_authorize(db, actor)
    value = await setting(db, "software_update")
    if value.get("id") != str(identity):
        raise ValueError("Unknown software update")
    if value.get("phase") == phase:
        if value.get("phase_details", {}) != details:
            raise ValueError("The phase was already recorded with different details")
        return value
    if phase not in TRANSITIONS.get(value["phase"], set()):
        raise ValueError("Invalid software update transition")
    maintenance = await setting(db, "maintenance")
    if not maintenance.get("enabled") or maintenance.get("update_id") != str(identity):
        raise ValueError("The software update no longer owns maintenance")
    expected = {
        "BACKED_UP": {"snapshot"},
        "MIGRATED": {"revision"},
        "READY": {"health_checked"},
        "FAILED": {"error_code"},
    }.get(phase, set())
    if set(details) != expected:
        raise ValueError("Unexpected software phase details")
    if phase == "BACKED_UP" and (
        not isinstance(details["snapshot"], str) or not SNAPSHOT_NAME.fullmatch(details["snapshot"])
    ):
        raise ValueError("A verified backup snapshot is required")
    if phase == "MIGRATED" and (
        not isinstance(details["revision"], str)
        or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", details["revision"])
    ):
        raise ValueError("A migrated database revision is required")
    if phase == "READY" and details["health_checked"] is not True:
        raise ValueError("All new services must pass health checks")
    if phase == "FAILED" and (
        not isinstance(details["error_code"], str)
        or not re.fullmatch(r"[A-Z_]{3,64}", details["error_code"])
    ):
        raise ValueError("Use a non-sensitive error code")
    value = {
        **value,
        **details,
        "phase": phase,
        "phase_details": details,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    await save_setting(db, "software_update", value, actor)
    if phase in TERMINAL:
        await save_setting(db, "maintenance", value["previous_maintenance"], actor)
    await record(db, "SOFTWARE_UPDATE_" + phase, value, actor)
    return value


async def export_audit_tail(db: AsyncSession, identity: UUID, actor: UUID) -> dict[str, Any]:
    await lock_and_authorize(db, actor)
    value = await setting(db, "software_update")
    if value.get("id") != str(identity) or value.get("phase") != "ROLLING_BACK":
        raise ValueError("Only an update being rolled back can export its audit tail")
    maintenance = await setting(db, "maintenance")
    if not maintenance.get("enabled") or maintenance.get("update_id") != str(identity):
        raise ValueError("Audit export requires update maintenance")
    anchor = value["audit_anchor"]
    if await audit_anchor(db, anchor["maximum"]) != anchor:
        raise ValueError("The original audit history has changed")
    rows = await db.stream_scalars(
        text("SELECT to_jsonb(a) FROM audit_log a WHERE id>:maximum ORDER BY id"),
        {"maximum": anchor["maximum"]},
        execution_options={"yield_per": 1000},
    )
    result: list[dict[str, Any]] = []
    size = 0
    async for row in rows:
        normalized = audit_row(row)
        size += len(canonical(normalized))
        if len(result) >= MAX_TAIL_ROWS or size > MAX_TAIL_BYTES:
            raise ValueError("Audit tail exceeds the offline transfer limit; database preserved")
        result.append(normalized)
    return {"schema": "dispatcher-update-audit-1", "update": value, "rows": result}


async def merge_audit_tail(
    db: AsyncSession, identity: UUID, actor: UUID, tail: dict[str, Any]
) -> dict[str, int]:
    """Caller verifies the file signature first and commits this entire transaction once."""
    await lock_and_authorize(db, actor)
    current = await setting(db, "software_update")
    maintenance = await setting(db, "maintenance")
    if (
        current.get("id") != str(identity)
        or not maintenance.get("enabled")
        or maintenance.get("update_id") != str(identity)
    ):
        raise ValueError("Restored database does not own this update")
    if not isinstance(tail, dict) or set(tail) != {"schema", "update", "rows"}:
        raise ValueError("Invalid audit transfer")
    incoming = tail["update"]
    if (
        tail["schema"] != "dispatcher-update-audit-1"
        or not isinstance(incoming, dict)
        or incoming.get("phase") != "ROLLING_BACK"
        or current.get("phase") in TERMINAL
        or any(
            incoming.get(key) != current.get(key)
            for key in (
                "id",
                "actor_id",
                "version",
                "package_sha256",
                "reason",
                "audit_anchor",
                "previous_maintenance",
                "started_at",
            )
        )
    ):
        raise ValueError("Audit transfer belongs to a different update or database")
    if not isinstance(tail["rows"], list) or len(tail["rows"]) > MAX_TAIL_ROWS:
        raise ValueError("Invalid audit tail size")
    rows = [audit_row(row) for row in tail["rows"]]
    if sum(len(canonical(row)) for row in rows) > MAX_TAIL_BYTES:
        raise ValueError("Invalid audit tail size")
    identifiers = [row["id"] for row in rows]
    anchor = current["audit_anchor"]
    if identifiers != sorted(set(identifiers)) or any(i <= anchor["maximum"] for i in identifiers):
        raise ValueError("Invalid audit tail identities")
    await db.execute(text("LOCK TABLE audit_log IN SHARE ROW EXCLUSIVE MODE"))
    if await audit_anchor(db, anchor["maximum"]) != anchor:
        raise ValueError("Restored audit history does not match the original prefix")
    pending = []
    for row in rows:
        existing = await db.scalar(
            text("SELECT to_jsonb(a) FROM audit_log a WHERE id=:id"), {"id": row["id"]}
        )
        if existing:
            if audit_row(existing) != row:
                raise ValueError("Conflicting audit identity; existing history was preserved")
        else:
            pending.append(row)
        if row["user_id"] and not await db.scalar(
            text("SELECT 1 FROM users WHERE id=CAST(:id AS uuid)"), {"id": row["user_id"]}
        ):
            raise ValueError("An audit actor is missing from the restored database")
    for row in pending:
        await db.execute(
            text(
                "INSERT INTO audit_log "
                "(id,user_id,action,entity_type,entity_id,payload,ip,created_at) "
                "VALUES (:id,CAST(:user_id AS uuid),:action,:entity_type,CAST(:entity_id AS uuid),"
                "CAST(:payload AS jsonb),CAST(:ip AS inet),CAST(:created_at AS timestamptz))"
            ),
            {
                **row,
                "payload": json.dumps(row["payload"]),
                "created_at": datetime.fromisoformat(row["created_at"]),
            },
        )
    await db.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('audit_log','id'), "
            "GREATEST((SELECT last_value FROM audit_log_id_seq), "
            "(SELECT coalesce(max(id),1) FROM audit_log)))"
        )
    )
    await save_setting(db, "software_update", incoming, actor)
    recorded = await db.scalar(
        text(
            "SELECT 1 FROM audit_log WHERE action='SOFTWARE_UPDATE_AUDIT_RESTORED' "
            "AND entity_id=:id"
        ),
        {"id": identity},
    )
    result = {"imported": len(pending), "already_present": len(rows) - len(pending)}
    if not recorded:
        await record(db, "SOFTWARE_UPDATE_AUDIT_RESTORED", {"id": str(identity), **result}, actor)
    return result


async def export_audit_file(
    db: AsyncSession, identity: UUID, actor: UUID, path: Path, key: bytes
) -> dict[str, int]:
    value = await export_audit_tail(db, identity, actor)
    await asyncio.to_thread(
        write_record, path, value, key, "software-update-audit-1", exclusive=True
    )
    return {"exported": len(value["rows"])}


async def merge_audit_file(
    db: AsyncSession, identity: UUID, actor: UUID, path: Path, key: bytes
) -> dict[str, int]:
    value = await asyncio.to_thread(
        read_record, path, key, "software-update-audit-1", limit=MAX_TAIL_BYTES + 2 * 1024 * 1024
    )
    return await merge_audit_tail(db, identity, actor, value)


async def update_status(db: AsyncSession, identity: UUID, actor: UUID) -> dict[str, Any]:
    await lock_and_authorize(db, actor)
    value = await setting(db, "software_update")
    if value.get("id") != str(identity):
        value = (
            await db.scalar(
                text(
                    "SELECT payload FROM audit_log WHERE entity_type='software_update' "
                    "AND entity_id=:id AND payload ? 'phase' ORDER BY id DESC LIMIT 1"
                ),
                {"id": identity},
            )
            or {}
        )
    if not value:
        return {"id": str(identity), "phase": "ABSENT"}
    return {key: value[key] for key in ("id", "phase", "snapshot", "revision") if key in value}


async def command(arguments: argparse.Namespace) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    try:
        async with AsyncSession(engine) as db, db.begin():
            if arguments.action == "begin":
                value = await begin_update(
                    db,
                    arguments.id,
                    arguments.actor,
                    arguments.version,
                    arguments.digest,
                    arguments.reason,
                )
                result: dict[str, Any] = {"id": value["id"], "phase": value["phase"]}
            elif arguments.action == "status":
                result = await update_status(db, arguments.id, arguments.actor)
            elif arguments.action == "advance":
                value = await advance_update(
                    db,
                    arguments.id,
                    arguments.actor,
                    arguments.phase,
                    json.loads(arguments.details),
                )
                result = {"id": value["id"], "phase": value["phase"]}
            else:
                key = (await asyncio.to_thread(arguments.signing_key.read_bytes)).strip()
                if arguments.action == "export-audit":
                    counts = await export_audit_file(
                        db, arguments.id, arguments.actor, arguments.file, key
                    )
                else:
                    counts = await merge_audit_file(
                        db, arguments.id, arguments.actor, arguments.file, key
                    )
                result = {"id": str(arguments.id), **counts}
        print(json.dumps(result))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("begin", "status", "advance", "export-audit", "merge-audit"):
        operation = commands.add_parser(name)
        operation.add_argument("--id", type=UUID, required=True)
        operation.add_argument("--actor", type=UUID, required=True)
        if name == "begin":
            operation.add_argument("--version", required=True)
            operation.add_argument("--digest", required=True)
            operation.add_argument("--reason", required=True)
        elif name == "advance":
            operation.add_argument("--phase", choices=TRANSITIONS, required=True)
            operation.add_argument("--details", default="{}")
        elif name != "status":
            operation.add_argument("--file", type=Path, required=True)
            operation.add_argument("--signing-key", type=Path, required=True)
    try:
        asyncio.run(command(parser.parse_args()))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Update database operation failed: {error}\n")
    except SQLAlchemyError:
        parser.exit(1, "Update database operation failed; inspect state before retrying.\n")


if __name__ == "__main__":
    main()
