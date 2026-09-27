import json
import logging
from typing import NoReturn

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.logging import JSONFormatter, RequestContextMiddleware
from app.main import app


@pytest.mark.asyncio
async def test_health_and_unique_request_ids() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/healthz")
        second = await client.get("/healthz")
    assert first.status_code == 200
    assert first.json() == {"status": "ok"}
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.asyncio
async def test_error_contract_and_request_id() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,allowed",
    [(origin, True) for origin in settings.cors_origins]
    + [("https://outside.example", False), ("null", False)],
)
async def test_cors_local_origins_only(origin: str, allowed: bool) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options(
            "/healthz", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
        )
    assert (response.headers.get("access-control-allow-origin") == origin) is allowed
    assert response.headers["x-request-id"]


def test_structured_log_fields() -> None:
    record = logging.LogRecord("test", logging.INFO, "test", 1, "Соединение", (), None)
    output = json.loads(JSONFormatter().format(record))
    assert set(output) >= {"ts", "level", "request_id", "user_id", "route", "duration_ms"}
    assert output["message"] == "Соединение"


@pytest.mark.asyncio
async def test_unhandled_error_has_request_id_without_internal_details() -> None:
    failing_app = FastAPI()
    failing_app.add_middleware(RequestContextMiddleware)

    @failing_app.get("/fail", response_model=None)
    async def fail() -> NoReturn:
        raise RuntimeError("private diagnostic detail")

    async with AsyncClient(
        transport=ASGITransport(app=failing_app), base_url="http://test"
    ) as client:
        response = await client.get("/fail")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert "private diagnostic detail" not in response.text
