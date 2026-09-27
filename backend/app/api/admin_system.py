import asyncio
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Annotated, Any
from uuid import UUID
from zipfile import BadZipFile

import httpx
from fastapi import APIRouter, Query, UploadFile
from sqlalchemy import func, literal, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, Admin, Cache
from app.api.errors import APIError
from app.config import settings
from app.db.models import AuditLog, Street
from app.domain.audit_visibility import technical_audit_payload
from app.domain.pagination import next_cursor, page_offset
from app.generation.llm import validate_local_llm_url
from app.seeds.import_classifier import import_classifier
from app.seeds.import_streets import import_streets
from app.transport_security import http_verify

router = APIRouter(prefix="/admin", tags=["admin"])


async def import_upload(file: UploadFile, kind: str, db: DB, user: Admin) -> dict[str, Any]:
    data = await file.read(10 * 1024 * 1024 + 1)
    if not data or len(data) > 10 * 1024 * 1024:
        raise APIError(400, "VALIDATION_ERROR", "Выберите непустой файл размером до 10 МБ.")
    with TemporaryDirectory(prefix="dispatcher-import-") as directory:
        path = Path(directory) / ("upload.xlsx" if kind == "classifier" else "upload.csv")
        await asyncio.to_thread(path.write_bytes, data)
        try:
            if kind == "classifier":
                result = await import_classifier(db, path, user.id)
            else:
                result = {"streets": await import_streets(db, path, user.id)}
            await db.commit()
        except (
            ValueError,
            KeyError,
            TypeError,
            BadZipFile,
            IntegrityError,
            StopIteration,
        ) as error:
            await db.rollback()
            raise APIError(
                400,
                "VALIDATION_ERROR",
                "Файл не соответствует формату справочника. "
                "Проверьте столбцы и значения по data/README.md.",
            ) from error
    return result


@router.post("/classifier/import")
async def classifier_upload(file: UploadFile, db: DB, user: Admin) -> dict[str, Any]:
    return await import_upload(file, "classifier", db, user)


@router.post("/streets/import")
async def streets_upload(file: UploadFile, db: DB, user: Admin) -> dict[str, Any]:
    return await import_upload(file, "streets", db, user)


@router.get("/streets")
async def streets(
    db: DB, user: Admin, limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: str | None = None
) -> dict[str, Any]:
    offset = page_offset(cursor)
    rows = list(
        await db.scalars(
            select(Street).order_by(Street.name, Street.id).offset(offset).limit(limit + 1)
        )
    )
    return {
        "items": [
            {"id": str(row.id), "name": row.name, "district": row.district} for row in rows[:limit]
        ],
        "next_cursor": next_cursor(offset, limit, len(rows) > limit),
        "total": await db.scalar(select(func.count()).select_from(Street)),
    }


@router.get("/audit")
async def audit(
    db: DB,
    user: Admin,
    user_id: UUID | None = None,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    if (from_at and from_at.tzinfo is None) or (to_at and to_at.tzinfo is None):
        raise APIError(400, "VALIDATION_ERROR", "Укажите часовой пояс даты.")
    if from_at and to_at and from_at > to_at:
        raise APIError(400, "VALIDATION_ERROR", "Начало периода должно предшествовать окончанию.")
    query = select(AuditLog)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if from_at:
        query = query.where(AuditLog.created_at >= from_at)
    if to_at:
        query = query.where(AuditLog.created_at <= to_at)
    offset = page_offset(cursor)
    rows = list(
        await db.scalars(
            query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    )
    return {
        "items": [
            {
                "id": row.id,
                "user_id": str(row.user_id) if row.user_id else None,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": str(row.entity_id) if row.entity_id else None,
                "payload": technical_audit_payload(row.action, row.payload),
                "ip": str(row.ip) if row.ip else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "next_cursor": next_cursor(offset, limit, len(rows) > limit),
    }


async def http_probe(url: str) -> dict[str, Any]:
    start = perf_counter()
    try:
        async with httpx.AsyncClient(timeout=1.5, verify=http_verify()) as client:
            response = await client.get(url)
        ok = response.status_code == 200
    except httpx.HTTPError:
        ok = False
    return {"ok": ok, "latency_ms": round((perf_counter() - start) * 1000)}


def last_backup() -> str | None:
    path = Path("/data/backup-status/manifest.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        date: str = value["last_backup_at"]
        datetime.fromisoformat(date)
        return date
    except (OSError, ValueError, KeyError, TypeError):
        return None


@router.get("/health")
async def health(db: DB, cache: Cache, user: Admin) -> dict[str, Any]:
    start = perf_counter()
    database = {
        "ok": await db.scalar(select(literal(1))) == 1,
        "latency_ms": round((perf_counter() - start) * 1000),
    }
    redis_ok = bool(await cache.ping())
    worker_ok = bool(await cache.get("arq:queue:health-check"))
    queue_depth = int(await cache.zcard("arq:queue"))
    language = await http_probe(settings.languagetool_url.rstrip("/") + "/v2/languages")
    generation: dict[str, Any] = {"kind": settings.generation_backend, "ok": True}
    if settings.generation_backend == "local_llm":
        try:
            url = validate_local_llm_url()
            generation.update(await http_probe(url + "/api/tags"))
        except ValueError:
            generation["ok"] = False
    return {
        "database": database,
        "redis": {"ok": redis_ok},
        "languagetool": language,
        "generation_backend": generation,
        "worker": {"ok": worker_ok, "queue_depth": queue_depth},
        "last_backup_at": await asyncio.to_thread(last_backup),
    }
