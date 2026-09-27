"""Administrator package verification and signed, explicitly local installation requests."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zipfile import BadZipFile

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.api.deps import DB, Admin
from app.api.errors import APIError
from app.config import settings
from app.db.models import AuditLog, SystemSetting
from app.domain.maintenance import MAINTENANCE_LOCK, maintenance_state
from app.operations import update_catalog as catalog

router = APIRouter(prefix="/admin/operations/updates", tags=["admin"])
CATALOG_LOCK = 112152


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    action: Literal["apply", "activate", "rollback"]
    package_id: UUID | None = None
    update_id: UUID | None = None
    reason: str = Field(min_length=5, max_length=1000)


async def trusted_key() -> bytes:
    try:
        key = await asyncio.to_thread(catalog.publisher)
        if key is not None:
            return key
    except (OSError, ValueError):
        raise APIError(
            503, "UPDATE_KEY_UNAVAILABLE", "Не удалось прочитать доверенный ключ издателя."
        ) from None
    raise APIError(
        409, "UPDATE_KEY_REQUIRED", "Сначала настройте доверенный ключ издателя на сервере."
    )


async def catalog_lock(db: DB) -> None:
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": CATALOG_LOCK}):
        raise APIError(
            409, "OPERATION_IN_PROGRESS", "Дождитесь завершения загрузки или изменения пакета."
        )
    if not await db.scalar(
        text("SELECT pg_try_advisory_xact_lock_shared(:id)"), {"id": MAINTENANCE_LOCK}
    ):
        raise APIError(409, "OPERATION_IN_PROGRESS", "Дождитесь завершения технической операции.")


async def package_rows(db: DB) -> list[SystemSetting]:
    return list(
        (
            await db.scalars(
                select(SystemSetting)
                .where(SystemSetting.key.startswith(catalog.PREFIX, autoescape=True))
                .order_by(SystemSetting.updated_at.desc())
                .limit(catalog.MAX_PACKAGES + 1)
            )
        ).all()
    )


async def package(db: DB, identity: UUID) -> SystemSetting:
    row = await db.get(SystemSetting, catalog.PREFIX + str(identity))
    if row is None:
        raise APIError(404, "NOT_FOUND", "Пакет обновления не найден.")
    return row


async def checked_package(row: SystemSetting) -> tuple[Path, dict[str, Any]]:
    key = await trusted_key()
    try:
        path = catalog.archive_path(UUID(row.value["id"]))
        verified = await asyncio.to_thread(catalog.verify_archive, path, key)
    except (OSError, ValueError, BadZipFile, KeyError):
        raise APIError(
            409,
            "UPDATE_PACKAGE_INVALID",
            "Пакет изменён, недоступен или подписан другим ключом. "
            "Удалите повреждённый пакет и загрузите проверенный заново.",
        ) from None
    if any(verified[name] != row.value[name] for name in verified):
        raise APIError(
            409, "UPDATE_PACKAGE_INVALID", "Пакет изменён после загрузки. Установка запрещена."
        )
    return path, verified


async def history(db: DB) -> list[dict[str, Any]]:
    records = (
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.action.in_(("SOFTWARE_UPDATE_REQUESTED", "SOFTWARE_UPDATE_STARTED")))
            .order_by(AuditLog.id.desc())
            .limit(100)
        )
    ).all()
    updates: dict[str, dict[str, Any]] = {}
    for record in records:
        value = record.payload or {}
        identity = str(value.get("update_id") or record.entity_id)
        if identity not in updates:
            updates[identity] = {
                "id": identity,
                "version": value.get("version", ""),
                "reason": value.get("reason", ""),
                "at": record.created_at.isoformat(),
                "phase": "REQUESTED",
            }
            if value.get("expires_at") and datetime.fromisoformat(
                value["expires_at"]
            ) <= datetime.now(UTC):
                updates[identity]["phase"] = "EXPIRED"
    if updates:
        phases = (
            await db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "software_update",
                    AuditLog.entity_id.in_([UUID(value) for value in updates]),
                    AuditLog.payload["phase"].astext.is_not(None),
                )
                .distinct(AuditLog.entity_id)
                .order_by(AuditLog.entity_id, AuditLog.id.desc())
            )
        ).all()
        for record in phases:
            value = record.payload or {}
            updates[str(record.entity_id)].update(
                phase=value["phase"], at=record.created_at.isoformat()
            )
    return list(updates.values())[:50]


@router.get("")
async def index(db: DB, user: Admin) -> dict[str, Any]:
    try:
        key = await asyncio.to_thread(catalog.publisher)
        trust = {
            "configured": key is not None,
            "fingerprint": hashlib.sha256(key).hexdigest() if key else None,
            "error": None,
        }
    except (OSError, ValueError):
        trust = {
            "configured": False,
            "fingerprint": None,
            "error": "Не удалось прочитать доверенный ключ издателя.",
        }
    current = await db.get(SystemSetting, "software_update")
    return {
        "trust": trust,
        "limits": {
            "archive_bytes": catalog.MAX_ARCHIVE,
            "packages": catalog.MAX_PACKAGES,
            "storage_bytes": catalog.MAX_STORAGE,
        },
        "packages": [row.value for row in await package_rows(db)],
        "updates": await history(db),
        "current": {
            name: current.value[name]
            for name in ("id", "phase", "version", "reason", "started_at", "phase_details")
            if name in current.value
        }
        if current
        else None,
        "maintenance": await maintenance_state(db),
        "execution": "LOCAL_COMMAND",
    }


@router.post("/packages", status_code=201)
async def upload(
    request: Request,
    db: DB,
    user: Admin,
    filename: Annotated[str, Query(min_length=1, max_length=128)],
) -> dict[str, Any]:
    if (
        Path(filename).name != filename
        or "\\" in filename
        or not filename.lower().endswith(".zip")
        or any(ord(c) < 32 for c in filename)
    ):
        raise APIError(400, "VALIDATION_ERROR", "Выберите ZIP-пакет программы.")
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise APIError(400, "VALIDATION_ERROR", "Отправьте файл пакета без изменения содержимого.")
    key = await trusted_key()
    await catalog_lock(db)
    maintenance = await maintenance_state(db)
    if any(maintenance.get(field) for field in ("update_id", "job_id", "switch_id", "topology_id")):
        raise APIError(
            409, "OPERATION_IN_PROGRESS", "Сначала завершите текущую техническую операцию."
        )
    rows = await package_rows(db)
    if len(rows) >= catalog.MAX_PACKAGES:
        raise APIError(409, "UPDATE_STORAGE_FULL", "Удалите ненужный пакет перед новой загрузкой.")
    identity = uuid4()
    destination = catalog.archive_path(identity)
    temporary = destination.with_suffix(".partial")
    size = 0
    published = False
    committed = False
    try:
        used = await asyncio.to_thread(catalog.storage_bytes)
        catalog.ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        with temporary.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > catalog.MAX_ARCHIVE or used + size > catalog.MAX_STORAGE:
                    raise APIError(
                        413,
                        "UPLOAD_TOO_LARGE",
                        "Пакет или каталог обновлений превышает допустимый объём.",
                    )
                await asyncio.to_thread(stream.write, chunk)
            await asyncio.to_thread(stream.flush)
            await asyncio.to_thread(os.fsync, stream.fileno())
        verified = await asyncio.to_thread(catalog.verify_archive, temporary, key)
        duplicate = next((row for row in rows if row.value["sha256"] == verified["sha256"]), None)
        if duplicate:
            await checked_package(duplicate)
            return duplicate.value
        value = {
            "id": str(identity),
            "filename": filename,
            "uploaded_at": datetime.now(UTC).isoformat(),
            **verified,
        }
        temporary.replace(destination)
        published = True
        db.add(SystemSetting(key=catalog.PREFIX + str(identity), value=value, updated_by=user.id))
        db.add(
            AuditLog(
                user_id=user.id,
                action="SOFTWARE_UPDATE_PACKAGE_VERIFIED",
                entity_type="software_package",
                entity_id=identity,
                payload=value,
            )
        )
        await db.commit()
        committed = True
        return value
    except (ValueError, BadZipFile):
        db.add(
            AuditLog(
                user_id=user.id,
                action="SOFTWARE_UPDATE_PACKAGE_REJECTED",
                entity_type="software_package",
                payload={
                    "filename": filename,
                    "size": size,
                    "error_code": "UPDATE_PACKAGE_INVALID",
                },
            )
        )
        await db.commit()
        raise APIError(
            400,
            "UPDATE_PACKAGE_INVALID",
            "Подпись или содержимое пакета не прошли проверку. "
            "Получите исправленный пакет у издателя.",
        ) from None
    except OSError:
        raise APIError(
            503,
            "UPDATE_STORAGE_UNAVAILABLE",
            "Не удалось сохранить пакет. Проверьте свободное место и повторите загрузку.",
        ) from None
    finally:
        temporary.unlink(missing_ok=True)
        if published and not committed:
            destination.unlink(missing_ok=True)


@router.get("/packages/{identity}")
async def download(identity: UUID, db: DB, user: Admin) -> FileResponse:
    row = await package(db, identity)
    path, _ = await checked_package(row)
    return FileResponse(
        path, filename=f"release-{row.value['version']}.zip", media_type="application/zip"
    )


@router.delete("/packages/{identity}", status_code=204)
async def remove(identity: UUID, db: DB, user: Admin) -> Response:
    await catalog_lock(db)
    current = await maintenance_state(db)
    if any(current.get(field) for field in ("update_id", "job_id", "switch_id", "topology_id")):
        raise APIError(
            409, "OPERATION_IN_PROGRESS", "Сначала завершите текущую техническую операцию."
        )
    pending = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == "SOFTWARE_UPDATE_REQUESTED",
                AuditLog.payload["package_id"].astext == str(identity),
                AuditLog.created_at > datetime.now(UTC) - timedelta(hours=24),
            )
        )
    ).all()
    terminal = {
        item["id"] for item in await history(db) if item["phase"] in {"ACTIVE", "ROLLED_BACK"}
    }
    if any(str((row.payload or {}).get("update_id")) not in terminal for row in pending):
        raise APIError(
            409,
            "OPERATION_IN_PROGRESS",
            "Пакет нужен действующему запросу на установку. "
            "Дождитесь результата или истечения запроса.",
        )
    row = await package(db, identity)
    path = catalog.archive_path(identity)
    db.add(
        AuditLog(
            user_id=user.id,
            action="SOFTWARE_UPDATE_PACKAGE_REMOVED",
            entity_type="software_package",
            entity_id=identity,
            payload={"sha256": row.value["sha256"], "version": row.value["version"]},
        )
    )
    await db.delete(row)
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        raise APIError(
            503,
            "UPDATE_STORAGE_UNAVAILABLE",
            "Не удалось удалить архив. Повторите действие после проверки хранилища.",
        ) from None
    await db.commit()
    return Response(status_code=204)


@router.post("/requests")
async def create_request(body: UpdateRequest, db: DB, user: Admin) -> dict[str, Any]:
    if body.action == "apply" and (body.package_id is None or body.update_id is not None):
        raise APIError(400, "VALIDATION_ERROR", "Выберите пакет для нового обновления.")
    await catalog_lock(db)
    key = await trusted_key()
    existing = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "SOFTWARE_UPDATE_REQUESTED", AuditLog.entity_id == body.id
        )
    )
    current = await db.get(SystemSetting, "software_update")
    if existing:
        value = existing.payload or {}
        expected = {
            "actor_id": str(user.id),
            "action": body.action,
            "package_id": str(body.package_id) if body.package_id else None,
            "update_id": str(body.update_id or body.id),
            "reason": body.reason,
        }
        if any(value.get(name) != item for name, item in expected.items()):
            raise APIError(
                409, "IDEMPOTENCY_CONFLICT", "Этот запрос уже использован с другими параметрами."
            )
        if value.get("publisher_sha256") != hashlib.sha256(key).hexdigest():
            raise APIError(
                409, "UPDATE_KEY_CHANGED", "Ключ издателя изменён. Подготовьте новый запрос."
            )
    else:
        maintenance = await maintenance_state(db)
        if any(maintenance.get(field) for field in ("job_id", "switch_id", "topology_id")):
            raise APIError(409, "OPERATION_IN_PROGRESS", "Сначала завершите техническую операцию.")
        if body.action == "apply":
            if body.package_id is None or body.update_id is not None:
                raise APIError(400, "VALIDATION_ERROR", "Выберите пакет для нового обновления.")
            if maintenance.get("update_id"):
                raise APIError(
                    409, "OPERATION_IN_PROGRESS", "Сначала завершите текущее обновление."
                )
            row = await package(db, body.package_id)
            _, verified = await checked_package(row)
            version, digest = verified["version"], verified["sha256"]
            update_id = body.id
        else:
            allowed = (
                {"READY"}
                if body.action == "activate"
                else {"STARTED", "BACKED_UP", "MIGRATED", "READY", "ROLLING_BACK", "FAILED"}
            )
            if (
                body.package_id is not None
                or body.update_id is None
                or current is None
                or current.value.get("id") != str(body.update_id)
                or current.value.get("phase") not in allowed
            ):
                raise APIError(
                    409,
                    "INVALID_TRANSITION",
                    "Текущее состояние обновления не допускает это действие. Обновите страницу.",
                )
            version, digest = current.value["version"], current.value["package_sha256"]
            update_id = body.update_id
        now = datetime.now(UTC)
        value = {
            "schema": catalog.REQUEST_PURPOSE,
            "id": str(body.id),
            "update_id": str(update_id),
            "action": body.action,
            "actor_id": str(user.id),
            "package_id": str(body.package_id) if body.package_id else None,
            "version": version,
            "package_sha256": digest,
            "publisher_sha256": hashlib.sha256(key).hexdigest(),
            "reason": body.reason,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
    try:
        envelope = await asyncio.to_thread(
            catalog.signed_request,
            value,
            catalog.request_key(catalog.TOKEN.read_bytes().strip(), settings.jwt_secret),
        )
    except (OSError, ValueError):
        raise APIError(
            409,
            "UPDATE_REQUEST_UNAVAILABLE",
            "Запрос истёк или ключ установки недоступен. "
            "Создайте новый запрос после проверки сервера.",
        ) from None
    if existing is None:
        db.add(
            AuditLog(
                user_id=user.id,
                action="SOFTWARE_UPDATE_REQUESTED",
                entity_type="software_update_request",
                entity_id=body.id,
                payload=value,
            )
        )
        await db.commit()
    return {
        "request": envelope,
        "filename": f"software-update-{body.id}.json",
        "execution": "LOCAL_COMMAND",
    }
