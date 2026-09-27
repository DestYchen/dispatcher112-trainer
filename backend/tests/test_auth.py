from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pyotp
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AuditLog, User


async def sign_in(
    client: AsyncClient, login: str = "student1", password: str = "student", totp: str | None = None
) -> dict[str, Any]:
    pre = await client.get("/api/v1/auth/me")
    csrf = pre.json()["error"]["details"]["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": password, "totp": totp},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    client.headers["X-CSRF-Token"] = payload["csrf_token"]
    return payload


async def test_session_roles_cookie_logout_and_audit(client: AsyncClient, db: AsyncSession) -> None:
    await sign_in(client)
    response = await client.get("/api/v1/auth/me")
    assert response.json()["role"] == "STUDENT"
    assert response.json()["service"]["code"] == "DDS_CHERTANOVO"
    cookie = client.cookies.get("session")
    assert cookie is not None
    claims = jwt.decode(cookie, settings.jwt_secret, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 28800
    assert (await client.get("/api/v1/teacher/lessons")).status_code == 403
    assert (await client.get("/api/v1/admin/classifier/stats")).status_code == 403
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    client.cookies.set("session", cookie)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    events = list(
        await db.scalars(select(AuditLog.action).where(AuditLog.action.in_(["LOGIN", "LOGOUT"])))
    )
    assert "LOGIN" in events and "LOGOUT" in events


async def test_csrf_required_even_on_login(client: AsyncClient) -> None:
    assert (
        await client.post("/api/v1/auth/login", json={"login": "student1", "password": "student"})
    ).status_code == 403
    await sign_in(client)
    client.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/v1/auth/logout")).status_code == 403


async def test_sixth_login_is_rate_limited_and_failures_audited(
    client: AsyncClient, db: AsyncSession
) -> None:
    bootstrap = await client.get("/api/v1/auth/me")
    client.headers["X-CSRF-Token"] = bootstrap.json()["error"]["details"]["csrf_token"]
    for _ in range(5):
        result = await client.post(
            "/api/v1/auth/login", json={"login": "student1", "password": "wrong"}
        )
        assert result.status_code == 401
    result = await client.post(
        "/api/v1/auth/login", json={"login": "student1", "password": "student"}
    )
    assert result.status_code == 429
    assert result.json()["error"]["code"] == "RATE_LIMITED"
    assert (
        len(list(await db.scalars(select(AuditLog).where(AuditLog.action == "LOGIN_FAILED")))) == 5
    )


async def test_totp_required_and_replay_rejected(client: AsyncClient, db: AsyncSession) -> None:
    student = (await db.scalars(select(User).where(User.login == "student1"))).one()
    student.totp_secret = pyotp.random_base32()
    await db.flush()
    pre = await client.get("/api/v1/auth/me")
    csrf = pre.json()["error"]["details"]["csrf_token"]
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"login": "student1", "password": "student"},
            headers={"X-CSRF-Token": csrf},
        )
    ).status_code == 401
    code = pyotp.TOTP(student.totp_secret).now()
    await sign_in(client, totp=code)
    await client.post("/api/v1/auth/logout")
    pre = await client.get("/api/v1/auth/me")
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"login": "student1", "password": "student", "totp": code},
            headers={"X-CSRF-Token": pre.json()["error"]["details"]["csrf_token"]},
        )
    ).status_code == 401


async def test_inactive_user_and_expired_session(client: AsyncClient, db: AsyncSession) -> None:
    await sign_in(client)
    user = (await db.scalars(select(User).where(User.login == "student1"))).one()
    user.is_active = False
    await db.flush()
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    token = jwt.encode(
        {
            "sub": str(user.id),
            "exp": datetime.now(UTC) - timedelta(seconds=1),
            "iat": datetime.now(UTC) - timedelta(hours=9),
            "jti": "expired",
            "csrf": "expired",
            "kind": "session",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    client.cookies.clear()
    client.cookies.set("session", token)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_cookie_flags_and_real_role_pages(client: AsyncClient) -> None:
    for login, password, role, url in [
        ("teacher", "teacher", "TEACHER", "/api/v1/teacher/lessons"),
        ("admin", "admin", "ADMIN", "/api/v1/admin/classifier/stats"),
    ]:
        client.cookies.clear()
        pre = await client.get("/api/v1/auth/me")
        response = await client.post(
            "/api/v1/auth/login",
            json={"login": login, "password": password},
            headers={"X-CSRF-Token": pre.json()["error"]["details"]["csrf_token"]},
        )
        assert response.json()["user"]["role"] == role
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert (await client.get(url)).status_code == 200
