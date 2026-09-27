import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_configuration import process_report
from app.api.deps import DB, Admin, Cache
from app.api.errors import APIError
from app.config import settings
from app.db.base import session_factory
from app.db.models import AuditLog, GenerationJob, Lesson, SystemSetting
from app.domain.maintenance import MAINTENANCE_LOCK, maintenance_state
from app.domain.runtime_configuration import read_snapshot
from app.operations import local_control
from app.operations.backup_client import backup_job, catalog
from app.operations.backup_protocol import BACKUP_KINDS, validate_request
from app.operations.client import control_request
from app.operations.docker import MIN_MEMORY, RESTART_ONLY, SERVICES

router = APIRouter(prefix="/admin", tags=["admin"])


class MaintenanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool
    reason: str = Field(min_length=5, max_length=1000)


class BackupSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    hour: int = Field(default=3, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    retention: int = Field(default=14, ge=14, le=90)


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    kind: Literal[
        "service",
        "resources",
        "backup_create",
        "backup_verify",
        "backup_configure",
        "backup_integrity_check",
        "backup_integrity_baseline",
    ]
    service: str = Field(pattern=r"^[a-z_]+$", max_length=32)
    action: Literal["start", "stop", "restart"] = "restart"
    cpus: float = Field(default=1, ge=0.5, le=64)
    memory_mb: int = Field(default=512, ge=128, le=65536)
    snapshot: str | None = Field(default=None, max_length=64)
    schedule: BackupSchedule | None = None
    reason: str | None = Field(default=None, min_length=5, max_length=1000)


async def exclusive(db: DB) -> None:
    if not await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MAINTENANCE_LOCK}
    ):
        raise APIError(
            409,
            "OPERATION_IN_PROGRESS",
            "Дождитесь завершения текущих изменений и повторите запрос.",
        )


@router.get("/maintenance")
async def maintenance(db: DB, user: Admin) -> dict[str, object]:
    return await maintenance_state(db)


@router.post("/maintenance")
async def set_maintenance(
    body: MaintenanceInput, db: DB, user: Admin, cache: Cache
) -> dict[str, object]:
    await exclusive(db)
    previous = await maintenance_state(db)
    recovery = await db.get(SystemSetting, "recovery")
    if not body.enabled and recovery and recovery.value.get("status") == "PREPARED":
        raise APIError(
            409,
            "RECOVERY_NOT_ACTIVATED",
            "Восстановление ещё не принято. "
            "Завершите проверку и активацию восстановленной системы.",
        )
    if any(previous.get(field) for field in ("job_id", "update_id", "switch_id", "topology_id")):
        raise APIError(
            409, "OPERATION_IN_PROGRESS", "Сначала дождитесь результата технической операции."
        )
    if not body.enabled:
        configuration = await read_snapshot(db)
        topology = await db.get(SystemSetting, "runtime_topology")
        expected = topology.value.get("backend_replicas", 1) if topology else 1
        if (configuration.revision or expected > 1) and not (
            await process_report(cache, configuration.revision, expected)
        )["applied"]:
            raise APIError(
                409,
                "CONFIGURATION_NOT_APPLIED",
                "Дождитесь применения параметров всеми рабочими процессами.",
            )
    if body.enabled and (
        await db.scalar(select(Lesson.id).where(Lesson.status == "RUNNING").limit(1))
        or await db.scalar(
            select(GenerationJob.id).where(GenerationJob.status == "RUNNING").limit(1)
        )
    ):
        raise APIError(
            409, "LESSON_RUNNING", "Сначала завершите занятия и дождитесь окончания генерации."
        )
    row = await db.get(SystemSetting, "maintenance")
    if row is None:
        row = SystemSetting(key="maintenance")
        db.add(row)
    current = body.model_dump()
    row.value, row.updated_by, row.updated_at = current, user.id, datetime.now(UTC)
    if bool(previous["enabled"]) != body.enabled:
        db.add(
            AuditLog(
                user_id=user.id,
                action="MAINTENANCE_CHANGED",
                entity_type="system_settings",
                payload={"previous": previous, "current": current},
            )
        )
    await db.commit()
    return current


@router.get("/operations/services")
async def services(user: Admin) -> dict[str, Any]:
    return await control_request("GET", "/services")


@router.get("/backups")
async def backups(user: Admin) -> dict[str, Any]:
    return await catalog()


@router.get("/operations/recovery")
async def recovery_status(db: DB, user: Admin) -> dict[str, Any]:
    row = await db.get(SystemSetting, "recovery_switch")
    recovery = await db.get(SystemSetting, "recovery")
    return {
        "actor_id": str(user.id),
        "switch": {key: value for key, value in row.value.items() if key != "previous_maintenance"}
        if row
        else None,
        "recovery": recovery.value if recovery else None,
    }


@router.get("/operations/services/{service}/logs")
async def logs(service: str, user: Admin) -> dict[str, Any]:
    if service not in SERVICES:
        raise APIError(404, "NOT_FOUND", "Сервис не найден.")
    return await control_request("GET", f"/services/{service}/logs")


@router.post("/operations/jobs", status_code=202)
async def start_operation(body: OperationInput, db: DB, user: Admin) -> dict[str, Any]:
    if body.service not in SERVICES:
        raise APIError(400, "VALIDATION_ERROR", "Сервис не найден.")
    if body.kind == "service" and body.service in RESTART_ONLY and body.action != "restart":
        raise APIError(400, "VALIDATION_ERROR", "Основной сервис можно только перезапустить.")
    if body.kind == "resources" and body.memory_mb < MIN_MEMORY.get(body.service, 128):
        raise APIError(400, "VALIDATION_ERROR", "Объём памяти ниже минимального для сервиса.")
    await exclusive(db)
    state = await maintenance_state(db)
    if any(state.get(field) for field in ("update_id", "switch_id", "topology_id")):
        raise APIError(409, "OPERATION_IN_PROGRESS", "Сначала завершите обновление программы.")
    if not state["enabled"]:
        raise APIError(
            409, "MAINTENANCE_REQUIRED", "Сначала включите режим технического обслуживания."
        )
    if state.get("job_id") and state["job_id"] != str(body.id):
        raise APIError(409, "OPERATION_IN_PROGRESS", "Дождитесь завершения предыдущей операции.")
    request = {**body.model_dump(mode="json", exclude_none=True), "actor_id": str(user.id)}
    if body.kind in BACKUP_KINDS:
        try:
            validate_request(request)
        except (ValueError, KeyError, TypeError):
            raise APIError(400, "VALIDATION_ERROR", "Проверьте параметры резервирования.") from None
    elif body.snapshot is not None or body.schedule is not None or body.reason is not None:
        raise APIError(400, "VALIDATION_ERROR", "Параметры резервирования здесь недопустимы.")
    recorded = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "TECHNICAL_OPERATION_REQUESTED", AuditLog.entity_id == body.id
        )
    )
    if recorded and recorded.payload != request:
        raise APIError(
            409, "IDEMPOTENCY_CONFLICT", "Идентификатор уже использован другой операцией."
        )
    if body.kind in BACKUP_KINDS and not recorded:
        available = await catalog()
        if not available["worker_ready"]:
            raise APIError(503, "BACKUP_UNAVAILABLE", "Исполнитель резервирования не отвечает.")
        if body.kind in {"backup_verify", "backup_integrity_baseline"} and body.snapshot not in {
            item["name"] for item in available["items"]
        }:
            raise APIError(404, "NOT_FOUND", "Резервная копия не найдена.")
        if body.kind == "backup_integrity_baseline":
            selected = next(item for item in available["items"] if item["name"] == body.snapshot)
            if (
                not selected.get("valid_manifest")
                or not selected.get("program_files")
                or not (selected.get("verification") or {}).get("verified_at")
            ):
                raise APIError(
                    409,
                    "BASELINE_NOT_VERIFIED",
                    "Сначала проверьте восстановление полной копии с программой.",
                )
    if not recorded:
        db.add(
            AuditLog(
                user_id=user.id,
                action="TECHNICAL_OPERATION_REQUESTED",
                entity_type="technical_operation",
                entity_id=body.id,
                payload=request,
            )
        )
    row = await db.get(SystemSetting, "maintenance")
    assert row is not None
    row.value = {**state, "job_id": str(body.id)}
    await db.commit()
    if body.kind in BACKUP_KINDS:
        return await backup_job(request)
    return await control_request("POST", "/jobs", request)


@router.get("/operations/jobs/{job_id}")
async def operation(job_id: UUID, db: DB, user: Admin) -> dict[str, Any]:
    return await operation_result(job_id, db)


@router.post("/operations/jobs/{job_id}/local-request")
async def local_request(job_id: UUID, db: DB, user: Admin) -> dict[str, Any]:
    await exclusive(db)
    state = await maintenance_state(db)
    if settings.technical_control_mode != "local" or state.get("job_id") != str(job_id):
        raise APIError(409, "INVALID_TRANSITION", "Выберите ожидающую локальную операцию.")
    recorded = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "TECHNICAL_OPERATION_REQUESTED", AuditLog.entity_id == job_id
        )
    )
    if not recorded or not recorded.payload or recorded.payload["kind"] in BACKUP_KINDS:
        raise APIError(404, "NOT_FOUND", "Локальная операция не найдена.")
    try:
        envelope = await asyncio.to_thread(local_control.export_request, recorded.payload)
    except (ValueError, OSError):
        raise APIError(
            409, "INVALID_TRANSITION", "Эту операцию уже выполняют или она завершена."
        ) from None
    return {"request": envelope, "filename": f"technical-operation-{job_id}.json"}


@router.post("/operations/jobs/{job_id}/cancel")
async def cancel_local_operation(job_id: UUID, db: DB, user: Admin) -> dict[str, Any]:
    await exclusive(db)
    if settings.technical_control_mode != "local":
        raise APIError(409, "INVALID_TRANSITION", "Отмена доступна для локальной очереди.")
    result = await control_request("GET", f"/jobs/{job_id}")
    if result["status"] != "QUEUED":
        raise APIError(409, "INVALID_TRANSITION", "Операция уже началась или завершена.")
    result.update(
        status="FAILED",
        error="Отменено администратором до выполнения.",
        finished_at=datetime.now(UTC).isoformat(),
    )
    await asyncio.to_thread(local_control.save_job, result)
    await db.commit()
    return await operation_result(job_id, db)


async def operation_result(job_id: UUID, db: AsyncSession) -> dict[str, Any]:
    recorded = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "TECHNICAL_OPERATION_REQUESTED", AuditLog.entity_id == job_id
        )
    )
    if not recorded or recorded.payload is None:
        raise APIError(404, "NOT_FOUND", "Операция не найдена.")
    if recorded.payload["kind"] in BACKUP_KINDS:
        result = await backup_job(recorded.payload)
    else:
        try:
            result = await control_request("GET", f"/jobs/{job_id}")
        except APIError as error:
            if error.code != "CONTROL_JOB_NOT_FOUND":
                raise
            result = await control_request("POST", "/jobs", recorded.payload)
    if result["status"] in {"SUCCEEDED", "FAILED"}:
        await exclusive(db)
        exists = await db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "TECHNICAL_OPERATION_FINISHED", AuditLog.entity_id == job_id
            )
        )
        if not exists:
            db.add(
                AuditLog(
                    user_id=recorded.user_id,
                    action="TECHNICAL_OPERATION_FINISHED",
                    entity_type="technical_operation",
                    entity_id=job_id,
                    payload=result,
                )
            )
        row = await db.get(SystemSetting, "maintenance")
        if row and row.value.get("job_id") == str(job_id):
            row.value = {key: value for key, value in row.value.items() if key != "job_id"}
        await db.commit()
    return result


async def reconcile_pending_operation(db: AsyncSession) -> None:
    state = await maintenance_state(db)
    if isinstance(state.get("job_id"), str):
        await operation_result(UUID(str(state["job_id"])), db)


async def monitor_operations() -> None:
    previous_error: str | None = None
    while True:
        try:
            async with session_factory() as db:
                await reconcile_pending_operation(db)
            previous_error = None
        except (APIError, OSError, SQLAlchemyError, ValueError) as error:
            kind = error.code if isinstance(error, APIError) else type(error).__name__
            if kind != previous_error:
                logging.getLogger(__name__).warning("Ожидание технической операции: %s", kind)
            previous_error = kind
        await asyncio.sleep(2)
