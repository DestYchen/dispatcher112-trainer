from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import jwt
import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import issue_token
from app.config import settings
from app.db.models import AuditLog, SystemSetting, User
from app.domain.access_policy import AccessPolicy
from app.main import app
from tests.test_auth import sign_in

teacher_routes = [
    (method, route.path)
    for route in app.routes
    if isinstance(route, APIRoute) and route.path.startswith("/api/v1/teacher/")
    for method in route.methods
    if method in {"GET", "POST", "PATCH", "DELETE"}
]


@pytest.mark.parametrize(("method", "path"), teacher_routes)
async def test_technical_admin_cannot_read_or_mutate_teaching(
    client: AsyncClient, method: str, path: str
) -> None:
    await sign_in(client, "admin", "admin")
    path = "/".join(str(uuid4()) if part.startswith("{") else part for part in path.split("/"))
    response = await client.request(method, path, json={} if method != "GET" else None)
    assert response.status_code == 403, (path, response.text)


async def test_policy_is_persistent_audited_and_idempotent(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "admin", "admin")
    original = (await client.get("/api/v1/admin/policies")).json()
    assert original["access"] == AccessPolicy().model_dump()
    assert original["audit_retention"] == "indefinite" and not original["audit_mutable"]
    policy = {**original["access"], "session_minutes": 60, "min_password_length": 12}
    for _ in range(2):
        assert (await client.patch("/api/v1/admin/policies", json=policy)).status_code == 200
    assert (await client.get("/api/v1/admin/policies")).json()["access"] == policy
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "ACCESS_POLICY_UPDATED")
        )
        == 1
    )


@pytest.mark.parametrize(
    "value",
    [
        {"session_minutes": 481},
        {"session_minutes": 14},
        {"session_minutes": "60"},
        {"min_password_length": 7},
        {"login_attempts": 6},
        {"lockout_minutes": 14},
        {"audit_mutable": True},
        {"require_admin_totp": "yes"},
    ],
)
async def test_policy_cannot_relax_baseline_or_modify_audit(
    client: AsyncClient, value: dict[str, Any]
) -> None:
    await sign_in(client, "admin", "admin")
    assert (await client.patch("/api/v1/admin/policies", json=value)).status_code == 400


async def test_password_policy_applies_to_creation_and_reset(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "admin", "admin")
    await client.patch("/api/v1/admin/policies", json={"min_password_length": 12})
    user_id = await db.scalar(select(User.id).where(User.login == "student1"))
    for path, body in [
        (f"/api/v1/admin/users/{user_id}/reset-password", {"password": "eight123"}),
        (
            "/api/v1/admin/users",
            {
                "login": "weakadmin",
                "password": "eight123",
                "first_name": "Тест",
                "last_name": "Тест",
                "role": "ADMIN",
            },
        ),
    ]:
        assert (await client.post(path, json=body)).status_code == 400
        assert (
            await client.post(path, json={**body, "password": "long-password123"})
        ).status_code in {200, 201}


async def test_shortened_policy_expires_preexisting_session(
    client: AsyncClient, db: AsyncSession
) -> None:
    student = await db.scalar(select(User).where(User.login == "student1"))
    assert student
    token, _ = issue_token("session", str(student.id))
    claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    claims["iat"] = datetime.now(UTC).timestamp() - 3601
    client.cookies.set("session", jwt.encode(claims, settings.jwt_secret, algorithm="HS256"))
    assert (await client.get("/api/v1/student/state")).status_code == 200
    db.add(SystemSetting(key="access_policy", value={"session_minutes": 60}))
    await db.flush()
    assert (await client.get("/api/v1/student/state")).status_code == 401


async def test_login_uses_configured_duration_and_attempt_limit(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(SystemSetting(key="access_policy", value={"session_minutes": 30, "login_attempts": 3}))
    await db.flush()
    await sign_in(client)
    claims = jwt.decode(client.cookies["session"], settings.jwt_secret, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 1800
    client.cookies.clear()
    for attempt in range(4):
        csrf = (await client.get("/api/v1/auth/me")).json()["error"]["details"]["csrf_token"]
        response = await client.post(
            "/api/v1/auth/login",
            json={"login": "unknown", "password": "wrong"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == (401 if attempt < 3 else 429)


async def test_totp_policy_does_not_lock_out_unconfigured_admin(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "admin", "admin")
    assert (
        await client.patch("/api/v1/admin/policies", json={"require_admin_totp": True})
    ).status_code == 409
    admin = await db.scalar(select(User).where(User.login == "admin"))
    assert admin
    admin.totp_secret = "JBSWY3DPEHPK3PXP"
    await db.flush()
    assert (
        await client.patch("/api/v1/admin/policies", json={"require_admin_totp": True})
    ).status_code == 200
    assert (
        await client.patch("/api/v1/admin/users", json={"id": str(admin.id), "totp_secret": None})
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/admin/users",
            json={
                "login": "otheradmin",
                "password": "long-password",
                "role": "ADMIN",
                "first_name": "Тест",
                "last_name": "Тест",
            },
        )
    ).status_code == 400
