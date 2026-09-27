import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, SystemSetting
from app.operations import control
from app.operations.docker import (
    containers,
    log_text,
    resource_limits,
    service_action,
    service_logs,
    status,
)
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


def container(
    service: str = "worker", project: str = "dispatcher112", oneoff: str = "False"
) -> dict[str, Any]:
    return {
        "Id": "abc123",
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
            "com.docker.compose.oneoff": oneoff,
        },
    }


async def test_controller_only_acts_on_project_services() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/containers/json":
            return httpx.Response(
                200,
                json=[
                    container(),
                    container(project="other"),
                    container(oneoff="True"),
                    container(service="control"),
                ],
            )
        if request.url.path.endswith("/restart"):
            return httpx.Response(204)
        return httpx.Response(
            200, json={"State": {"Running": True, "Health": {"Status": "healthy"}}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker"
    ) as client:
        assert len(await containers(client)) == 1
        result = await service_action(client, "worker", "restart")
        assert result["instances"] == 1
        for service, action in [
            ("postgres", "stop"),
            ("control", "restart"),
            ("../other", "start"),
            ("worker", "exec"),
        ]:
            before = len(requests)
            with pytest.raises(ValueError):
                await service_action(client, service, action)
            assert len(requests) == before
    assert all(request.url.path != "/containers/create" for request in requests)


async def test_resource_limits_are_bounded_and_sent_to_docker() -> None:
    updates = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            updates.append(json.loads(request.content))
            return httpx.Response(200, json={"Warnings": []})
        return httpx.Response(200, json=[container("languagetool")])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker"
    ) as client:
        for cpus, memory in [(0.1, 1024), (65, 1024), (1, 512), (1, 65537)]:
            with pytest.raises(ValueError):
                await resource_limits(client, "languagetool", cpus, memory)
        assert updates == []
        await resource_limits(client, "languagetool", 2, 1024)
    assert updates == [{"NanoCpus": 2_000_000_000, "Memory": 1024**3, "MemorySwap": 1024**3}]


async def test_service_logs_demultiplex_and_redact_secrets() -> None:
    message = b"message secret-value\n"
    framed = b"\x01\x00\x00\x00" + len(message).to_bytes(4, "big") + message
    assert log_text(framed) == message.decode()
    assert log_text(b"plain text") == "plain text"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[container()])
        if request.url.path.endswith("/logs"):
            assert request.url.params["tail"] == "100"
            return httpx.Response(200, content=framed)
        return httpx.Response(
            200, json={"Config": {"Env": ["JWT_SECRET=secret-value", "PUBLIC=message"]}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker"
    ) as client:
        result = await service_logs(client, "worker")
    assert result["items"][0]["text"] == "message [скрыто]\n"


async def test_private_controller_auth_retry_conflict_and_persistent_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("private-token", encoding="utf-8")
    monkeypatch.setattr(control, "TOKEN", token)
    monkeypatch.setattr(control, "ROOT", tmp_path)
    calls = []

    async def action(client: httpx.AsyncClient, service: str, action: str) -> dict[str, Any]:
        calls.append((service, action))
        return {"service": service, "action": action}

    monkeypatch.setattr(control, "service_action", action)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control.app), base_url="http://control"
    ) as client:
        assert (await client.get("/services")).status_code == 403
        client.headers["Authorization"] = "Bearer private-token"
        body = {
            "id": str(uuid4()),
            "actor_id": str(uuid4()),
            "kind": "service",
            "service": "worker",
        }
        assert (await client.post("/jobs", json=body)).status_code == 202
        await asyncio.gather(*control.pending)
        assert (await client.post("/jobs", json=body)).status_code == 202
        result = await client.get("/jobs/" + body["id"])
        assert result.json()["status"] == "SUCCEEDED" and len(calls) == 1
        assert (await client.post("/jobs", json={**body, "service": "ollama"})).status_code == 409


async def test_controller_restart_does_not_claim_interrupted_job_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "ROOT", tmp_path)
    identity = uuid4()
    control.save_job(identity, {"id": str(identity), "status": "RUNNING"})
    async with control.lifespan(control.app):
        assert control.read_job(identity)["status"] == "FAILED"


async def test_running_lesson_prevents_maintenance(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await lesson_fixture(db, 1)
    await sign_in(client, "admin", "admin")
    result = await client.post(
        "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Плановое обслуживание"}
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "LESSON_RUNNING"
    assert not (await client.get("/api/v1/admin/maintenance")).json()["enabled"]


async def test_maintenance_blocks_mutations_until_disabled(client: httpx.AsyncClient) -> None:
    await sign_in(client, "admin", "admin")
    for enabled in (True, False):
        assert (
            await client.post(
                "/api/v1/admin/maintenance",
                json={"enabled": enabled, "reason": "Плановое обслуживание"},
            )
        ).status_code == 200
        assert (await client.get("/api/v1/admin/users")).status_code == 200
        response = await client.patch("/api/v1/admin/policies", json={})
        assert response.status_code == (503 if enabled else 200)


async def test_operations_require_maintenance_and_are_idempotent_audited(
    client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(client, "admin", "admin")
    body = {"id": str(uuid4()), "kind": "service", "service": "worker", "action": "restart"}
    assert (await client.post("/api/v1/admin/operations/jobs", json=body)).status_code == 409
    jobs: dict[str, Any] = {}

    async def request(
        method: str, path: str, value: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            assert value
            jobs[value["id"]] = {"id": value["id"], "status": "SUCCEEDED", "request": value}
            return jobs[value["id"]]  # type: ignore[no-any-return]
        return jobs[path.rsplit("/", 1)[1]]  # type: ignore[no-any-return]

    monkeypatch.setattr("app.api.admin_operations.control_request", request)
    await client.post(
        "/api/v1/admin/maintenance", json={"enabled": True, "reason": "Тест операций"}
    )
    for _ in range(2):
        assert (await client.post("/api/v1/admin/operations/jobs", json=body)).status_code == 202
    assert (
        await client.post("/api/v1/admin/operations/jobs", json={**body, "service": "ollama"})
    ).status_code == 409
    assert (
        await client.post(
            "/api/v1/admin/maintenance", json={"enabled": False, "reason": "Тест завершён"}
        )
    ).status_code == 409
    for _ in range(2):
        assert (await client.get("/api/v1/admin/operations/jobs/" + body["id"])).json()[
            "status"
        ] == "SUCCEEDED"
    for event in ("TECHNICAL_OPERATION_REQUESTED", "TECHNICAL_OPERATION_FINISHED"):
        assert (
            await db.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == event)
            )
            == 1
        )
    assert (
        await client.post(
            "/api/v1/admin/maintenance", json={"enabled": False, "reason": "Тест завершён"}
        )
    ).status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        {"service": "other"},
        {"service": "control"},
        {"service": "postgres", "action": "stop"},
        {"kind": "resources", "service": "ollama", "memory_mb": 512},
    ],
)
async def test_operation_boundary_rejects_invalid_requests(
    client: httpx.AsyncClient, db: AsyncSession, value: dict[str, Any]
) -> None:
    await sign_in(client, "admin", "admin")
    db.add(SystemSetting(key="maintenance", value={"enabled": True}))
    await db.flush()
    response = await client.post(
        "/api/v1/admin/operations/jobs",
        json={"id": str(uuid4()), "kind": "service", "service": "worker", **value},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("running", [True, False])
async def test_monitoring_uses_cpu_interval_and_hides_container_environment(running: bool) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/stats"):
            assert request.url.params["stream"] == "false"
            assert "one-shot" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "cpu_stats": {
                        "system_cpu_usage": 1000,
                        "online_cpus": 6,
                        "cpu_usage": {"total_usage": 250},
                    },
                    "precpu_stats": {"system_cpu_usage": 900, "cpu_usage": {"total_usage": 200}},
                    "memory_stats": {"usage": 123456},
                },
            )
        return httpx.Response(
            200,
            json={
                "State": {"Running": running, "Status": "running" if running else "exited"},
                "RestartCount": 2,
                "Image": "sha256:test-image",
                "HostConfig": {"Memory": 2048 * 1024**2, "NanoCpus": 2_000_000_000},
                "Config": {"Env": ["JWT_SECRET=private-value"]},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker"
    ) as client:
        result = await status(client, container())
    assert result["cpu_percent"] == (300 if running else None)
    assert result["memory_bytes"] == (123456 if running else None)
    assert result["cpu_limit"] == 2 and result["restart_count"] == 2
    assert len(requests) == (2 if running else 1)
    assert "private-value" not in json.dumps(result) and "Env" not in result
