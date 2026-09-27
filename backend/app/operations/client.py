import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.api.errors import APIError
from app.config import settings
from app.operations import local_control
from app.operations.backup_protocol import read_signed
from app.transport_security import http_verify


async def control_request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        if settings.technical_control_mode == "local":
            if method == "GET" and path == "/services":
                return await asyncio.to_thread(local_control.report)
            if method == "GET" and path.startswith("/services/") and path.endswith("/logs"):
                return await asyncio.to_thread(local_control.report, path.split("/")[2])
            if method == "POST" and path == "/jobs" and body is not None:
                return await asyncio.to_thread(local_control.job, body)
            if method == "GET" and path.startswith("/jobs/"):
                from uuid import UUID

                identity = UUID(path.rsplit("/", 1)[1])
                target = local_control.ROOT / "jobs" / f"{identity}.json"
                if not target.exists():
                    raise APIError(404, "CONTROL_JOB_NOT_FOUND", "Операция ещё не подготовлена.")
                return await asyncio.to_thread(
                    read_signed, target, local_control.key(), "technical-host-result"
                )
            raise ValueError("Unsupported local control request")
        token = await asyncio.to_thread(Path("/run/control/token").read_text, encoding="utf-8")
        async with httpx.AsyncClient(timeout=30, trust_env=False, verify=http_verify()) as client:
            response = await client.request(
                method,
                settings.control_url + path,
                json=body,
                headers={"Authorization": "Bearer " + token.strip()},
            )
        if response.status_code == 404:
            raise APIError(404, "CONTROL_JOB_NOT_FOUND", "Операция ещё не поступила контроллеру.")
        response.raise_for_status()
        value: dict[str, Any] = response.json()
        return value
    except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        raise APIError(
            503,
            "CONTROL_UNAVAILABLE",
            "Технический контроллер недоступен. Проверьте его состояние и повторите запрос.",
        ) from error
