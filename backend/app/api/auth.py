import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated, Any
from uuid import uuid4

import jwt
import pyotp
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, Cache, check_csrf, current_user, decode_session
from app.api.errors import APIError
from app.config import settings
from app.db.models import AuditLog, Service, User
from app.domain.access_policy import access_policy
from app.domain.security import password_hasher

router = APIRouter(prefix="/auth", tags=["auth"])
DUMMY_HASH = password_hasher.hash(token_urlsafe(24))


class LoginInput(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, pattern=r"^\d{6}$")


def issue_token(
    kind: str, user_id: str | None = None, session_seconds: int = 28800
) -> tuple[str, str]:
    now = datetime.now(UTC)
    csrf = token_urlsafe(32)
    token = jwt.encode(
        {
            "sub": user_id or "preauth",
            "kind": kind,
            "csrf": csrf,
            "jti": uuid4().hex,
            "iat": now.timestamp(),
            "exp": now.timestamp() + (session_seconds if kind == "session" else 900),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return token, csrf


async def user_payload(db: AsyncSession, user: User) -> dict[str, Any]:
    service = await db.get(Service, user.service_id) if user.service_id else None
    full_name = " ".join(filter(None, [user.last_name, user.first_name, user.middle_name]))
    short_name = f"{user.last_name} {user.first_name[0]}." + (
        f" {user.middle_name[0]}." if user.middle_name else ""
    )
    return {
        "id": str(user.id),
        "login": user.login,
        "full_name": full_name,
        "short_name": short_name,
        "role": user.role,
        "service": {"code": service.code, "name": service.name} if service else None,
    }


@router.get("/me")
async def me(request: Request, db: DB, cache: Cache) -> JSONResponse:
    try:
        user = await current_user(request, db, cache)
    except APIError as error:
        if error.status != 401:
            raise
        token, csrf = issue_token("preauth")
        response = JSONResponse(
            {
                "error": {
                    "code": "UNAUTHENTICATED",
                    "message": "Войдите в учебную систему.",
                    "details": {"csrf_token": csrf},
                    "request_id": request.state.request_id,
                }
            },
            status_code=401,
        )
        response.set_cookie(
            "preauth",
            token,
            httponly=True,
            samesite="strict",
            max_age=900,
            secure=settings.cookie_secure,
        )
        return response
    return JSONResponse(
        {
            **await user_payload(db, user),
            "csrf_token": request.state.session["csrf"],
            "server_time": datetime.now(UTC).isoformat(),
        }
    )


@router.post("/login")
async def login(body: LoginInput, request: Request, db: DB, cache: Cache) -> JSONResponse:
    try:
        payload = decode_session(request.cookies.get("preauth"), "preauth")
    except APIError as error:
        raise APIError(
            403, "FORBIDDEN", "Обновите страницу входа для получения защитного токена."
        ) from error
    check_csrf(request, payload)
    login_name = body.login.strip().lower()
    policy = await access_policy(db)
    key = "login_limit:" + sha256(login_name.encode()).hexdigest()
    async with cache.pipeline(transaction=True) as pipeline:
        pipeline.incr(key)
        pipeline.expire(key, policy.lockout_minutes * 60, nx=True)
        attempts = (await pipeline.execute())[0]
    if attempts > policy.login_attempts:
        db.add(
            AuditLog(
                action="LOGIN_RATE_LIMITED",
                payload={"login": login_name},
                ip=request.client.host if request.client else None,
            )
        )
        await db.commit()
        raise APIError(
            429,
            "RATE_LIMITED",
            f"Слишком много попыток. Повторите через {policy.lockout_minutes} минут.",
        )
    user = await db.scalar(select(User).where(User.login == login_name))
    try:
        valid: bool = await asyncio.to_thread(
            password_hasher.verify, user.password_hash if user else DUMMY_HASH, body.password
        )
    except VerificationError:
        valid = False
    if user and user.totp_secret:
        valid = (
            valid
            and bool(body.totp)
            and pyotp.TOTP(user.totp_secret).verify(body.totp or "", valid_window=1)
        )
        if valid:
            valid = bool(await cache.set(f"totp_used:{user.id}:{body.totp}", "1", ex=90, nx=True))
    if user and user.role == "ADMIN" and policy.require_admin_totp and not user.totp_secret:
        valid = False
    if user is None or not user.is_active or not valid:
        db.add(
            AuditLog(
                user_id=user.id if user else None,
                action="LOGIN_FAILED",
                payload={"login": login_name},
                ip=request.client.host if request.client else None,
            )
        )
        await db.commit()
        raise APIError(401, "UNAUTHENTICATED", "Неверный логин или пароль.")
    # Only failed attempts should count toward the lockout: a student who signs in again
    # several times during a lesson must not be locked out.
    await cache.delete(key)
    session_seconds = policy.session_minutes * 60
    token, csrf = issue_token("session", str(user.id), session_seconds)
    db.add(
        AuditLog(
            user_id=user.id, action="LOGIN", ip=request.client.host if request.client else None
        )
    )
    await db.commit()
    response = JSONResponse({"user": await user_payload(db, user), "csrf_token": csrf})
    response.set_cookie(
        "session",
        token,
        httponly=True,
        samesite="strict",
        max_age=session_seconds,
        secure=settings.cookie_secure,
    )
    response.delete_cookie("preauth", httponly=True, samesite="strict")
    return response


@router.post("/logout", status_code=204)
async def logout(
    request: Request, db: DB, cache: Cache, user: Annotated[User, Depends(current_user)]
) -> Response:
    await cache.set("revoked:" + request.state.session["jti"], "1", ex=28800)
    db.add(
        AuditLog(
            user_id=user.id, action="LOGOUT", ip=request.client.host if request.client else None
        )
    )
    await db.commit()
    response = Response(status_code=204)
    response.delete_cookie("session", httponly=True, samesite="strict")
    return response
