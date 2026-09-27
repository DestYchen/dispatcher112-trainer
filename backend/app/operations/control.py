"""Private control API. Only this container has access to the Docker socket."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.operations.docker import (
    docker_client,
    resource_limits,
    service_action,
    service_logs,
    service_status,
)

ROOT = Path("/data/operations")
TOKEN = Path("/control/token")
pending: set[asyncio.Task[None]] = set()
mutation_lock = asyncio.Lock()


def authorize(authorization: Annotated[str, Header()] = "") -> None:
    expected = "Bearer " + TOKEN.read_text(encoding="utf-8").strip()
    if not compare_digest(authorization, expected):
        raise HTTPException(403, "Доступ к техническому контроллеру закрыт.")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    def recover() -> None:
        for path in (ROOT / "jobs").glob("*.json"):
            identity = UUID(path.stem)
            value = read_job(identity)
            if value["status"] in {"QUEUED", "RUNNING"}:
                value.update(
                    status="FAILED",
                    finished_at=datetime.now(UTC).isoformat(),
                    error="Контроллер был перезапущен. "
                    "Проверьте фактическое состояние сервиса перед новой операцией.",
                )
                save_job(identity, value)

    await asyncio.to_thread(recover)
    yield


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(authorize)],
    lifespan=lifespan,
)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    actor_id: UUID
    kind: Literal["service", "resources"]
    service: str = Field(pattern=r"^[a-z_]+$", max_length=32)
    action: Literal["start", "stop", "restart"] = "restart"
    cpus: float = Field(default=1, ge=0.5, le=64)
    memory_mb: int = Field(default=512, ge=128, le=65536)


def job_path(job_id: UUID) -> Path:
    return ROOT / "jobs" / f"{job_id}.json"


def save_job(job_id: UUID, value: dict[str, Any]) -> None:
    path = job_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_job(job_id: UUID) -> dict[str, Any]:
    path = job_path(job_id)
    if not path.is_file():
        raise HTTPException(404, "Операция не найдена.")
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


async def execute(body: Action) -> None:
    async with mutation_lock:
        value = await asyncio.to_thread(read_job, body.id)
        value.update(status="RUNNING", started_at=datetime.now(UTC).isoformat())
        await asyncio.to_thread(save_job, body.id, value)
        try:
            async with docker_client() as client:
                if body.kind == "service":
                    result = await service_action(client, body.service, body.action)
                else:
                    result = await resource_limits(client, body.service, body.cpus, body.memory_mb)

                    def persist() -> None:
                        path = ROOT / "resources.json"
                        values = (
                            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                        )
                        values[body.service] = {"cpus": body.cpus, "memory_mb": body.memory_mb}
                        temporary = path.with_suffix(".partial")
                        temporary.write_text(json.dumps(values), encoding="utf-8")
                        temporary.replace(path)

                    await asyncio.to_thread(persist)
            value.update(status="SUCCEEDED", result=result)
        except (ValueError, httpx.HTTPError, OSError) as error:
            # Exception responses must never expose container environment or Docker payloads.
            value.update(
                status="FAILED",
                error=str(error)
                if isinstance(error, ValueError)
                else "Контроллер не смог завершить операцию. Проверьте состояние сервиса.",
            )
        value["finished_at"] = datetime.now(UTC).isoformat()
        await asyncio.to_thread(save_job, body.id, value)


@app.get("/services")
async def services() -> dict[str, Any]:
    async with docker_client() as client:
        return await service_status(client)


@app.get("/services/{service}/logs")
async def logs(service: str) -> dict[str, Any]:
    try:
        async with docker_client() as client:
            return await service_logs(client, service)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/jobs", status_code=202)
async def start_job(body: Action) -> dict[str, Any]:
    # Atomic creation closes races from retries and two browser tabs.
    async with mutation_lock:
        if await asyncio.to_thread(job_path(body.id).exists):
            existing = await asyncio.to_thread(read_job, body.id)
            if existing["request"] != body.model_dump(mode="json"):
                raise HTTPException(409, "Идентификатор операции уже использован.")
            return existing
        value = {
            "id": str(body.id),
            "status": "QUEUED",
            "request": body.model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(save_job, body.id, value)
        task = asyncio.create_task(execute(body))
        pending.add(task)
        task.add_done_callback(pending.discard)
        return value


@app.get("/jobs/{job_id}")
async def job(job_id: UUID) -> dict[str, Any]:
    return await asyncio.to_thread(read_job, job_id)
