import asyncio
import json
import struct
from time import monotonic
from typing import Any

import httpx

from app.operations.service_policy import MIN_MEMORY as MIN_MEMORY
from app.operations.service_policy import RESTART_ONLY as RESTART_ONLY
from app.operations.service_policy import SERVICES as SERVICES


def docker_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
        base_url="http://docker/v1.45",
        timeout=30,
        trust_env=False,
    )


async def containers(client: httpx.AsyncClient, service: str | None = None) -> list[dict[str, Any]]:
    if service is not None and service not in SERVICES:
        raise ValueError("Неизвестный сервис.")
    labels = ["com.docker.compose.project=dispatcher112", "com.docker.compose.oneoff=False"]
    if service:
        labels.append(f"com.docker.compose.service={service}")
    response = await client.get(
        "/containers/json", params={"all": "true", "filters": json.dumps({"label": labels})}
    )
    response.raise_for_status()
    return [
        row
        for row in response.json()
        if row.get("Labels", {}).get("com.docker.compose.service") in SERVICES
        and row.get("Labels", {}).get("com.docker.compose.project") == "dispatcher112"
        and row.get("Labels", {}).get("com.docker.compose.oneoff") == "False"
        and (service is None or row["Labels"]["com.docker.compose.service"] == service)
    ]


async def status(client: httpx.AsyncClient, row: dict[str, Any]) -> dict[str, Any]:
    identity = row["Id"]
    response = await client.get(f"/containers/{identity}/json")
    response.raise_for_status()
    info = response.json()
    usage: dict[str, Any] = {}
    if info["State"]["Running"]:
        sample = await client.get(f"/containers/{identity}/stats", params={"stream": "false"})
        sample.raise_for_status()
        usage = sample.json()
    memory = usage.get("memory_stats", {})
    cpu = usage.get("cpu_stats", {})
    previous = usage.get("precpu_stats", {})
    system_delta = cpu.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get(
        "total_usage", 0
    )
    percent = (
        cpu_delta / system_delta * cpu.get("online_cpus", 1) * 100 if system_delta > 0 else None
    )
    service = row["Labels"]["com.docker.compose.service"]
    return {
        "id": identity[:12],
        "service": service,
        "state": info["State"]["Status"],
        "health": info["State"].get("Health", {}).get("Status"),
        "started_at": info["State"].get("StartedAt"),
        "restart_count": info["RestartCount"],
        "image_id": info["Image"],
        "cpu_percent": round(percent, 2) if percent is not None else None,
        "memory_bytes": memory.get("usage"),
        "memory_limit_bytes": info["HostConfig"]["Memory"],
        "cpu_limit": info["HostConfig"].get("NanoCpus", 0) / 1_000_000_000,
        "actions": ["restart"] if service in RESTART_ONLY else ["start", "stop", "restart"],
        "minimum_memory_mb": MIN_MEMORY.get(service, 128),
    }


async def service_status(client: httpx.AsyncClient) -> dict[str, Any]:
    rows = await containers(client)
    items = await asyncio.gather(*(status(client, row) for row in rows))
    info = await client.get("/info")
    info.raise_for_status()
    host = info.json()
    return {
        "items": items,
        "host": {
            "cpus": host["NCPU"],
            "memory_bytes": host["MemTotal"],
            "engine_version": host["ServerVersion"],
        },
    }


async def service_action(client: httpx.AsyncClient, service: str, action: str) -> dict[str, Any]:
    if action not in {"start", "stop", "restart"} or (
        service in RESTART_ONLY and action != "restart"
    ):
        raise ValueError("Для основного сервиса доступен только перезапуск.")
    rows = await containers(client, service)
    if not rows:
        raise ValueError("Контейнер сервиса не найден.")
    for row in rows:
        response = await client.post(f"/containers/{row['Id']}/{action}", params={"t": 10})
        response.raise_for_status()
        deadline = monotonic() + 180
        while True:
            response = await client.get(f"/containers/{row['Id']}/json")
            response.raise_for_status()
            state = response.json()["State"]
            ready = (
                (not state["Running"])
                if action == "stop"
                else (
                    state["Running"]
                    and state.get("Health", {}).get("Status", "healthy") == "healthy"
                )
            )
            if ready:
                break
            if monotonic() >= deadline:
                raise ValueError("Сервис не достиг ожидаемого состояния за три минуты.")
            await asyncio.sleep(0.5)
    return {"service": service, "action": action, "instances": len(rows)}


async def resource_limits(
    client: httpx.AsyncClient, service: str, cpus: float, memory_mb: int
) -> dict[str, Any]:
    if not 0.5 <= cpus <= 64 or not MIN_MEMORY.get(service, 128) <= memory_mb <= 65536:
        raise ValueError("Недопустимые ограничения ресурсов сервиса.")
    rows = await containers(client, service)
    if not rows:
        raise ValueError("Контейнер сервиса не найден.")
    for row in rows:
        response = await client.post(
            f"/containers/{row['Id']}/update",
            json={
                "NanoCpus": int(cpus * 1_000_000_000),
                "Memory": memory_mb * 1024 * 1024,
                "MemorySwap": memory_mb * 1024 * 1024,
            },
        )
        response.raise_for_status()
        if response.json().get("Warnings"):
            raise ValueError("Docker не смог применить все ограничения ресурсов.")
    return {"service": service, "cpus": cpus, "memory_mb": memory_mb}


def log_text(data: bytes) -> str:
    chunks: list[bytes] = []
    offset = 0
    while (
        offset + 8 <= len(data)
        and data[offset] in {0, 1, 2}
        and data[offset + 1 : offset + 4] == b"\x00\x00\x00"
    ):
        length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        if offset + 8 + length > len(data):
            break
        chunks.append(data[offset + 8 : offset + 8 + length])
        offset += 8 + length
    return (b"".join(chunks) if chunks else data).decode("utf-8", errors="replace")


async def service_logs(client: httpx.AsyncClient, service: str) -> dict[str, Any]:
    result = []
    for row in await containers(client, service):
        identity = row["Id"]
        info = await client.get(f"/containers/{identity}/json")
        info.raise_for_status()
        response = await client.get(
            f"/containers/{identity}/logs",
            params={"stdout": "true", "stderr": "true", "tail": 100, "timestamps": "true"},
        )
        response.raise_for_status()
        content = log_text(response.content)
        for variable in info.json()["Config"].get("Env", []):
            name, _, value = variable.partition("=")
            if value and any(
                word in name.upper() for word in ("PASSWORD", "SECRET", "TOKEN", "DATABASE_URL")
            ):
                content = content.replace(value, "[скрыто]")
        result.append({"instance": identity[:12], "text": content[-32768:]})
    return {"items": result}
