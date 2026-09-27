"""Installation-only recovery state changes in a newly restored database."""

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def prepare_database(db: AsyncSession, snapshot: str, instance: str) -> dict[str, Any]:
    if not re.fullmatch(r"backup-\d{8}T\d{6}Z-[a-f0-9]{12}", snapshot):
        raise ValueError("Invalid snapshot identifier")
    if not re.fullmatch(r"dispatcher_recovery_[a-f0-9]{12}", instance):
        raise ValueError("Invalid recovery instance")
    await db.execute(text("SELECT pg_advisory_xact_lock(112150)"))
    existing = await db.scalar(text("SELECT value FROM system_settings WHERE key='recovery'"))
    if existing and existing.get("instance") == instance:
        raise ValueError("This database has already been prepared for recovery")
    audit_count = await db.scalar(text("SELECT count(*) FROM audit_log"))
    audit_max = await db.scalar(text("SELECT coalesce(max(id),0) FROM audit_log"))
    jobs = list(
        await db.scalars(
            text(
                "UPDATE generation_jobs SET status='FAILED', "
                "error='Прервано восстановлением системы', "
                "updated_at=now() WHERE status IN ('QUEUED', 'RUNNING') RETURNING id"
            )
        )
    )
    calls = list(
        await db.scalars(
            text(
                "UPDATE sip_calls SET state='FAILED', ended_at=now() "
                "WHERE state IN ('REQUESTED', 'RINGING', 'CONNECTED') RETURNING id"
            )
        )
    )
    for entity, identities in (("generation_job", jobs), ("sip_call", calls)):
        for identity in identities:
            await db.execute(
                text(
                    "INSERT INTO audit_log (action, entity_type, entity_id, payload) "
                    "VALUES ('SYSTEM_RECOVERY_INTERRUPTED', :entity, :identity, "
                    "CAST(:payload AS jsonb))"
                ),
                {
                    "entity": entity,
                    "identity": identity,
                    "payload": json.dumps({"snapshot": snapshot}),
                },
            )
    previous = await db.scalar(text("SELECT value FROM system_settings WHERE key='maintenance'"))
    if previous and previous.get("job_id"):
        await db.execute(
            text(
                "INSERT INTO audit_log (action, entity_type, entity_id, payload) "
                "SELECT 'TECHNICAL_OPERATION_FINISHED', 'system', CAST(:identity AS uuid), "
                "CAST(:payload AS jsonb) WHERE NOT EXISTS ("
                "SELECT 1 FROM audit_log WHERE action='TECHNICAL_OPERATION_FINISHED' "
                "AND entity_id=CAST(:identity AS uuid))"
            ),
            {
                "identity": previous["job_id"],
                "payload": json.dumps({"status": "FAILED", "reason": "RECOVERY_INTERRUPTED"}),
            },
        )
    value = {
        "snapshot": snapshot,
        "instance": instance,
        "status": "PREPARED",
        "prepared_at": datetime.now(UTC).isoformat(),
        "source_audit_count": audit_count,
        "source_audit_max": audit_max,
        "interrupted_generation": len(jobs),
        "interrupted_calls": len(calls),
    }
    for key, payload in (
        ("recovery", value),
        ("maintenance", {"enabled": True, "reason": "Проверка восстановленной системы"}),
    ):
        await db.execute(
            text(
                "INSERT INTO system_settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
                "updated_by=NULL, updated_at=now()"
            ),
            {"key": key, "value": json.dumps(payload)},
        )
    await db.execute(
        text(
            "INSERT INTO audit_log (action, entity_type, payload) "
            "VALUES ('SYSTEM_RECOVERY_PREPARED', 'system', CAST(:payload AS jsonb))"
        ),
        {"payload": json.dumps(value)},
    )
    return value


async def activate_database(db: AsyncSession) -> dict[str, Any]:
    await db.execute(text("SELECT pg_advisory_xact_lock(112150)"))
    value = await db.scalar(
        text("SELECT value FROM system_settings WHERE key='recovery' FOR UPDATE")
    )
    if value and value.get("status") == "ACTIVE":
        return dict(value)
    if not value or value.get("status") != "PREPARED":
        raise ValueError("Only a prepared recovery database can be activated")
    maintenance = await db.scalar(text("SELECT value FROM system_settings WHERE key='maintenance'"))
    if not maintenance or not maintenance.get("enabled") or maintenance.get("job_id"):
        raise ValueError("Recovery requires maintenance without a pending operation")
    value = {**value, "status": "ACTIVE", "activated_at": datetime.now(UTC).isoformat()}
    await db.execute(
        text(
            "UPDATE system_settings SET value=CAST(:value AS jsonb), updated_at=now() "
            "WHERE key='recovery'"
        ),
        {"value": json.dumps(value)},
    )
    await db.execute(
        text(
            "UPDATE system_settings SET value=CAST(:value AS jsonb), "
            "updated_by=NULL, updated_at=now() WHERE key='maintenance'"
        ),
        {"value": json.dumps({"enabled": False, "reason": "Восстановление проверено"})},
    )
    await db.execute(
        text(
            "INSERT INTO audit_log (action, entity_type, payload) "
            "VALUES ('SYSTEM_RECOVERY_ACTIVATED', 'system', CAST(:payload AS jsonb))"
        ),
        {"payload": json.dumps(value)},
    )
    return dict(value)


async def run(action: str, snapshot: str | None, instance: str | None) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    try:
        async with AsyncSession(engine) as db, db.begin():
            if action == "prepare":
                if snapshot is None or instance is None:
                    raise ValueError("A snapshot and instance are required")
                value = await prepare_database(db, snapshot, instance)
            else:
                value = await activate_database(db)
        print(json.dumps(value))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "activate"))
    parser.add_argument("--snapshot")
    parser.add_argument("--instance")
    arguments = parser.parse_args()
    asyncio.run(run(arguments.action, arguments.snapshot, arguments.instance))


if __name__ == "__main__":
    main()
