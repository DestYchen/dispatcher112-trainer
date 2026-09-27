import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.api.errors import APIError
from app.operations.backup_protocol import enqueue, read_signed

ROOT = Path("/data/backup-operations")
STATUS = Path("/data/backup-status")
TOKEN = Path("/run/backup-control/token")


async def file_integrity() -> dict[str, Any]:
    def read() -> dict[str, Any]:
        key = TOKEN.read_bytes().strip()
        pulse = read_signed(STATUS / "worker.json", key, "heartbeat")
        now = datetime.now(UTC)
        age = (now - datetime.fromisoformat(pulse["at"])).total_seconds()
        if not 0 <= age < 30:
            raise ValueError("Integrity executor is unavailable")
        result = read_signed(STATUS / "file-integrity.json", key, "file-integrity")
        if result.get("status") not in {"OK", "FAIL", "EMPTY", "UNAVAILABLE", "RUNNING"}:
            raise ValueError("Unknown integrity status")
        moment = result["started_at"] if result["status"] == "RUNNING" else result["checked_at"]
        age = (now - datetime.fromisoformat(moment)).total_seconds()
        if not 0 <= age < (180 if result["status"] == "RUNNING" else 600):
            return {**result, "status": "UNAVAILABLE", "code": "STALE_RESULT"}
        return result

    try:
        return await asyncio.to_thread(read)
    except (OSError, ValueError, KeyError, TypeError):
        raise APIError(
            503,
            "INTEGRITY_UNAVAILABLE",
            "Не удалось получить подтверждённый результат проверки файлов.",
        ) from None


async def catalog() -> dict[str, Any]:
    def read() -> dict[str, Any]:
        key = TOKEN.read_bytes().strip()
        pulse = read_signed(STATUS / "worker.json", key, "heartbeat")
        alive = datetime.fromisoformat(pulse["at"]) > datetime.now(UTC) - timedelta(seconds=30)
        value = read_signed(STATUS / "catalog.json", key, "catalog")
        return {**value, "worker_ready": alive}

    try:
        return await asyncio.to_thread(read)
    except (OSError, ValueError, KeyError, TypeError):
        raise APIError(
            503, "BACKUP_UNAVAILABLE", "Сервис резервного копирования недоступен."
        ) from None


async def backup_job(request: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        key = TOKEN.read_bytes().strip()
        return enqueue(ROOT, request, key)

    try:
        return await asyncio.to_thread(run)
    except (OSError, ValueError, KeyError, TypeError):
        raise APIError(
            503, "BACKUP_UNAVAILABLE", "Не удалось прочитать задание резервирования."
        ) from None
