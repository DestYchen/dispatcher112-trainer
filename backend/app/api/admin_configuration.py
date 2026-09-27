"""Audited operational configuration under maintenance, with process acknowledgements."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from xml.etree.ElementTree import ParseError

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy import select, text

from app.api.deps import DB, Admin, Cache
from app.api.errors import APIError
from app.db.models import AuditLog, SystemSetting
from app.domain import configuration_xml
from app.domain.maintenance import MAINTENANCE_LOCK, maintenance_state
from app.domain.runtime_configuration import (
    KEY,
    ConfigurationSnapshot,
    RuntimeConfiguration,
    read_snapshot,
)
from app.runtime_configuration import process_status

router = APIRouter(prefix="/admin/operations/configuration", tags=["admin"])


class ConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=5, max_length=1000)
    configuration: RuntimeConfiguration


async def process_report(cache: Cache, revision: int, expected_backends: int = 1) -> dict[str, Any]:
    try:
        return await process_status(cache, revision, expected_backends)
    except (RedisError, ValueError, KeyError, TypeError):
        raise APIError(
            503,
            "CONFIGURATION_STATUS_UNAVAILABLE",
            "Не удалось проверить применение параметров. Повторите запрос.",
        ) from None


async def connection_budget(db: DB) -> int:
    return int(
        await db.scalar(
            text(
                "SELECT current_setting('max_connections')::int "
                "- current_setting('superuser_reserved_connections')::int - 10"
            )
        )
        or 0
    )


async def result(db: DB, cache: Cache, snapshot: ConfigurationSnapshot) -> dict[str, Any]:
    topology = await db.get(SystemSetting, "runtime_topology")
    expected = topology.value.get("backend_replicas", 1) if topology else 1
    live = await process_report(cache, snapshot.revision, expected)
    replicas = max(
        1,
        expected,
        sum(row["role"] == "backend" for row in live["processes"]),
    )
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "configured": snapshot.revision > 0,
        "maintenance": await maintenance_state(db),
        "database_connection_budget": await connection_budget(db),
        **live,
        "backend_replicas": replicas,
        "topology": topology.value if topology else None,
    }


@router.get("")
async def index(db: DB, cache: Cache, user: Admin) -> dict[str, Any]:
    return {**await result(db, cache, await read_snapshot(db)), "actor_id": str(user.id)}


@router.get("/export.xml")
async def export_xml(db: DB, user: Admin) -> Response:
    snapshot = await read_snapshot(db)
    db.add(
        AuditLog(
            user_id=user.id,
            action="RUNTIME_CONFIGURATION_EXPORTED",
            entity_type="system_settings",
            payload={"revision": snapshot.revision, "format": "XML"},
        )
    )
    await db.commit()
    return Response(
        configuration_xml.encode(snapshot.configuration),
        media_type="application/xml",
        headers={
            "Content-Disposition": 'attachment; filename="runtime-configuration.xml"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/import.xml")
async def import_xml(request: Request, db: DB, user: Admin) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise APIError(413, "VALIDATION_ERROR", "XML-файл должен быть не больше 64 КиБ.")
    try:
        configuration = configuration_xml.decode(bytes(raw))
    except (ValueError, UnicodeError, ParseError):
        raise APIError(
            400, "VALIDATION_ERROR", "Проверьте структуру XML и допустимые значения параметров."
        ) from None
    snapshot = await read_snapshot(db)
    return {
        "configuration": configuration.model_dump(mode="json"),
        "revision": snapshot.revision,
        "applied": False,
    }


@router.post("")
async def save(body: ConfigurationInput, db: DB, cache: Cache, user: Admin) -> dict[str, Any]:
    if not await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MAINTENANCE_LOCK}
    ):
        raise APIError(409, "OPERATION_IN_PROGRESS", "Дождитесь завершения текущего изменения.")
    previous = await read_snapshot(db)
    existing = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "RUNTIME_CONFIGURATION_CHANGED",
            AuditLog.entity_id == body.id,
        )
    )
    if existing:
        payload = existing.payload or {}
        if (
            existing.user_id != user.id
            or payload.get("reason") != body.reason
            or payload["previous"]["revision"] != body.expected_revision
            or payload["current"]["configuration"] != body.configuration.model_dump(mode="json")
        ):
            raise APIError(
                409, "IDEMPOTENCY_CONFLICT", "Идентификатор уже использован с другими параметрами."
            )
        return {
            **await result(db, cache, previous),
            "saved_revision": payload["current"]["revision"],
            "superseded": previous.revision != payload["current"]["revision"],
            "replayed": True,
        }
    maintenance = await maintenance_state(db)
    if any(maintenance.get(field) for field in ("job_id", "update_id", "switch_id", "topology_id")):
        raise APIError(409, "OPERATION_IN_PROGRESS", "Сначала завершите техническую операцию.")
    if not maintenance["enabled"]:
        raise APIError(409, "MAINTENANCE_REQUIRED", "Сначала включите режим обслуживания.")
    if previous.revision != body.expected_revision:
        raise APIError(
            409,
            "CONFIGURATION_CONFLICT",
            "Параметры изменены другим администратором. Загрузите текущую редакцию.",
        )
    if await db.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM lessons WHERE status='RUNNING') OR "
            "EXISTS(SELECT 1 FROM generation_jobs WHERE status IN ('QUEUED','RUNNING')) OR "
            "EXISTS(SELECT 1 FROM sip_calls WHERE state IN ('REQUESTED','RINGING','CONNECTED'))"
        )
    ):
        raise APIError(
            409,
            "OPERATION_IN_PROGRESS",
            "Завершите занятия, генерацию и вызовы перед изменением параметров.",
        )
    available = await connection_budget(db)
    topology = await db.get(SystemSetting, "runtime_topology")
    configured_nodes = topology.value.get("backend_replicas", 1) if topology else 1
    live = await process_report(cache, previous.revision)
    backend_nodes = max(
        configured_nodes, sum(row["role"] == "backend" for row in live["processes"]), 1
    )
    parameters = body.configuration.database
    required = parameters.connection_limit() + (backend_nodes - 1) * (
        parameters.api_pool_size + parameters.max_overflow
    )
    if required > available:
        raise APIError(
            400,
            "CONFIGURATION_CAPACITY",
            f"Суммарный лимит пулов превышает доступный бюджет БД: {available}.",
        )
    snapshot = ConfigurationSnapshot(
        revision=previous.revision + 1,
        configuration=body.configuration,
        request_id=body.id,
        actor_id=user.id,
        reason=body.reason,
        changed_at=datetime.now(UTC),
    )
    row = await db.get(SystemSetting, KEY)
    if row is None:
        row = SystemSetting(key=KEY)
        db.add(row)
    row.value, row.updated_by, row.updated_at = (
        snapshot.model_dump(mode="json"),
        user.id,
        datetime.now(UTC),
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="RUNTIME_CONFIGURATION_CHANGED",
            entity_type="system_settings",
            entity_id=body.id,
            payload={
                "previous": previous.model_dump(mode="json"),
                "current": snapshot.model_dump(mode="json"),
                "reason": body.reason,
            },
        )
    )
    await db.commit()
    return {
        **await result(db, cache, snapshot),
        "saved_revision": snapshot.revision,
        "superseded": False,
        "replayed": False,
    }
