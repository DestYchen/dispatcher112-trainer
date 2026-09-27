"""Technical evidence without teaching content or arbitrary log/SQL access."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic.script import ScriptDirectory
from sqlalchemy import String, cast, func, literal, select, text, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.api.errors import APIError
from app.db.base import Base, session_factory
from app.db.models import Assignment, AuditLog, GenerationJob, Lesson, SipCall, User, Workstation
from app.domain.pagination import next_cursor
from app.operations.backup_client import catalog

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
EXPORT_LIMIT = 10000
MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic"


def migration_heads() -> set[str]:
    return set(ScriptDirectory(str(MIGRATIONS)).get_heads())


def report_period(
    start: datetime | None, end: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    if any(value is not None and value.tzinfo is None for value in (start, end)):
        raise APIError(400, "VALIDATION_ERROR", "Укажите часовой пояс даты.")
    end = (end or now).astimezone(UTC)
    start = (start or end - timedelta(days=1)).astimezone(UTC)
    if not timedelta(seconds=1) <= end - start <= timedelta(days=366):
        raise APIError(400, "VALIDATION_ERROR", "Выберите период от одной секунды до 366 дней.")
    return start, end


async def save_http_failure(payload: dict[str, Any]) -> None:
    # Separate transaction: the failed request may have rolled its own work back.
    try:
        async with asyncio.timeout(0.5), session_factory() as db:
            db.add(AuditLog(action="SYSTEM_HTTP_ERROR", entity_type="system", payload=payload))
            await db.commit()
    except (TimeoutError, OSError, SQLAlchemyError) as error:
        metrics.diagnostic_write_failures += 1
        logging.getLogger(__name__).warning(
            "System error journal write failed: %s", type(error).__name__
        )


def safe_http_details(payload: dict[str, Any], routes: set[str]) -> dict[str, Any]:
    request_id = str(payload.get("request_id", ""))
    code = str(payload.get("code", ""))
    method = str(payload.get("method", ""))
    route = str(payload.get("route", ""))
    status = payload.get("status")
    return {
        "request_id": request_id if re.fullmatch(r"[a-f0-9]{32}", request_id) else None,
        "code": code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "HTTP_ERROR",
        "method": method if method in METHODS else "OTHER",
        "route": route if route in routes else "unmatched",
        "status": status if type(status) is int and 500 <= status <= 599 else None,
    }


async def integrity_checks(db: AsyncSession) -> list[dict[str, Any]]:
    expected = await asyncio.to_thread(migration_heads)
    actual = set(await db.scalars(text("SELECT version_num FROM alembic_version")))
    trigger = (
        await db.execute(
            text(
                "SELECT t.tgenabled::text AS enabled, t.tgtype, t.tgqual IS NULL AS unconditional, "
                "p.prosrc, l.lanname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
                "JOIN pg_language l ON l.oid=p.prolang WHERE "
                "t.tgrelid='public.audit_log'::regclass AND t.tgname='audit_immutable' "
                "AND NOT t.tgisinternal"
            )
        )
    ).first()
    audit_ok = bool(
        trigger
        and trigger.enabled in {"O", "A"}
        and trigger.tgtype == 27  # BEFORE UPDATE OR DELETE, FOR EACH ROW.
        and trigger.unconditional
        and trigger.lanname == "plpgsql"
        and " ".join(trigger.prosrc.split())
        == "BEGIN RAISE EXCEPTION 'audit_log is append-only'; END;"
    )
    checks: list[dict[str, Any]] = [
        {"code": "schema", "status": "OK" if actual == expected else "FAIL"},
        {"code": "audit", "status": "OK" if audit_ok else "FAIL"},
    ]
    for role, code, createdb in (
        ("dispatcher_app", "application_role", False),
        ("dispatcher_backup", "backup_role", True),
    ):
        # PostgreSQL can reorder AND predicates: CASE must guard type-specific ACL functions.
        row = (
            await db.execute(
                text(
                    "SELECT NOT (rolsuper OR rolcreaterole OR rolreplication OR rolbypassrls) "
                    "AND rolcreatedb=:createdb AND "
                    "NOT has_schema_privilege(rolname, 'public', 'CREATE') AND "
                    "NOT has_table_privilege(rolname, 'public.audit_log', "
                    "'UPDATE,DELETE,TRUNCATE,TRIGGER') AND "
                    "has_table_privilege(rolname, 'public.audit_log', 'SELECT') AND "
                    "NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) AND "
                    "NOT EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() "
                    "AND datdba=pg_roles.oid) AND "
                    "NOT EXISTS(SELECT 1 FROM pg_shdepend d WHERE "
                    "d.refclassid='pg_authid'::regclass AND d.refobjid=pg_roles.oid "
                    "AND d.deptype='o' AND d.dbid=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database())) AND "
                    "NOT has_any_column_privilege(rolname, 'public.audit_log', 'UPDATE') AND "
                    "NOT EXISTS(SELECT 1 FROM pg_class c WHERE "
                    "c.relnamespace='public'::regnamespace "
                    "AND CASE WHEN c.relkind IN ('r','p','v','m','f') THEN ("
                    "has_table_privilege(rolname,c.oid,'TRUNCATE,REFERENCES,TRIGGER') OR "
                    "(:createdb AND (has_table_privilege(rolname,c.oid,'INSERT,UPDATE,DELETE') OR "
                    "has_any_column_privilege(rolname,c.oid,'INSERT,UPDATE,REFERENCES')))) "
                    "ELSE false END) AND "
                    "NOT EXISTS(SELECT 1 FROM pg_class c WHERE "
                    "c.relnamespace='public'::regnamespace "
                    "AND CASE WHEN c.relkind='S' THEN ("
                    "has_sequence_privilege(rolname,c.oid,'UPDATE') OR "
                    "(:createdb AND has_sequence_privilege(rolname,c.oid,'USAGE'))) "
                    "ELSE false END) AND "
                    "NOT has_table_privilege(rolname,'public.alembic_version',"
                    "'INSERT,UPDATE,DELETE') "
                    "AS safe FROM pg_roles WHERE rolname=:role"
                ),
                {"role": role, "createdb": createdb},
            )
        ).first()
        checks.append({"code": code, "status": "OK" if row and row.safe else "FAIL"})
    references = (
        await db.execute(
            text(
                "SELECT t.relname AS table_name, u.relname AS target_name, c.convalidated, "
                "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(num,position) "
                "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num "
                "ORDER BY k.position) AS columns, "
                "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(num,position) "
                "JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num "
                "ORDER BY k.position) AS target_columns "
                "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                "JOIN pg_class u ON u.oid=c.confrelid "
                "WHERE c.connamespace='public'::regnamespace AND c.contype='f'"
            )
        )
    ).all()
    expected_references = {
        (
            table.name,
            tuple(element.parent.name for element in key.elements),
            key.referred_table.name,
            tuple(element.column.name for element in key.elements),
        )
        for table in Base.metadata.tables.values()
        for key in table.foreign_key_constraints
    }
    actual_references = {
        (row.table_name, tuple(row.columns), row.target_name, tuple(row.target_columns))
        for row in references
    }
    references_ok = expected_references <= actual_references and all(
        row.convalidated for row in references
    )
    checks.append({"code": "foreign_keys", "status": "OK" if references_ok else "FAIL"})
    try:
        backups = await catalog()
        items = backups["items"]
        checks.append(
            {
                "code": "backup_catalog",
                "status": (
                    "EMPTY"
                    if not items
                    else "OK"
                    if all(item["valid_manifest"] for item in items)
                    else "FAIL"
                ),
                "last_verified_at": max(
                    (
                        item["verification"]["verified_at"]
                        for item in items
                        if item.get("verification")
                    ),
                    default=None,
                ),
            }
        )
    except APIError:
        checks.append({"code": "backup_catalog", "status": "UNAVAILABLE"})
    return checks


async def technical_report(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    routes: set[str],
    offset: int = 0,
    limit: int = 50,
    export: bool = False,
) -> dict[str, Any]:
    usage: dict[str, int] = {}
    for name, column in (
        ("lessons_started", Lesson.started_at),
        ("assignments_delivered", Assignment.delivered_at),
        ("assignments_closed", Assignment.closed_at),
        ("sip_calls", SipCall.created_at),
        ("generation_jobs", GenerationJob.created_at),
        ("audit_events", AuditLog.created_at),
    ):
        usage[name] = int(
            await db.scalar(select(func.count()).where(column >= start, column < end)) or 0
        )
    usage["active_users"] = int(
        await db.scalar(
            select(func.count(func.distinct(AuditLog.user_id))).where(
                AuditLog.created_at >= start, AuditLog.created_at < end
            )
        )
        or 0
    )
    inventory = {
        "users_total": int(await db.scalar(select(func.count()).select_from(User)) or 0),
        "users_active": int(await db.scalar(select(func.count()).where(User.is_active)) or 0),
        "workstations_active": int(
            await db.scalar(select(func.count()).where(Workstation.is_active)) or 0
        ),
        "lessons_running": int(
            await db.scalar(select(func.count()).where(Lesson.status == "RUNNING")) or 0
        ),
    }
    failures = union_all(
        select(
            AuditLog.created_at.label("at"),
            literal("HTTP").label("source"),
            cast(AuditLog.id, String).label("reference"),
            AuditLog.payload.label("details"),
        ).where(AuditLog.action == "SYSTEM_HTTP_ERROR"),
        select(
            AuditLog.created_at,
            literal("OPERATION"),
            cast(AuditLog.entity_id, String),
            literal(None),
        ).where(
            AuditLog.action == "TECHNICAL_OPERATION_FINISHED",
            AuditLog.payload["status"].astext == "FAILED",
        ),
        select(
            GenerationJob.updated_at,
            literal("GENERATION"),
            cast(GenerationJob.id, String),
            literal(None),
        ).where(GenerationJob.status == "FAILED"),
        select(
            func.coalesce(SipCall.ended_at, SipCall.created_at),
            literal("SIP"),
            cast(SipCall.id, String),
            literal(None),
        ).where(SipCall.state == "FAILED"),
    ).subquery()
    query = select(failures).where(failures.c.at >= start, failures.c.at < end)
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if export:
        if total > EXPORT_LIMIT:
            raise APIError(
                400, "VALIDATION_ERROR", "В отчёте более 10 000 сбоев. Сократите период."
            )
        offset, limit = 0, EXPORT_LIMIT
    rows = (
        await db.execute(
            query.order_by(failures.c.at.desc(), failures.c.source, failures.c.reference)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    if export:
        # A transaction that began earlier may have committed between COUNT and SELECT.
        # Never label a truncated export as complete, even in that race.
        if len(rows) > EXPORT_LIMIT:
            raise APIError(
                400, "VALIDATION_ERROR", "В отчёте более 10 000 сбоев. Сократите период."
            )
        total = len(rows)
    items = [
        {
            "at": row.at.isoformat(),
            "source": row.source,
            "reference": row.reference,
            **(safe_http_details(row.details or {}, routes) if row.source == "HTTP" else {}),
        }
        for row in rows[:limit]
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "usage": usage,
        "inventory": inventory,
        "http": metrics.request_summary(),
        "integrity": await integrity_checks(db),
        "failures": {
            "total": total,
            "items": items,
            "next_cursor": next_cursor(offset, limit, len(rows) > limit),
        },
    }
