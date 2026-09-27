from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from hmac import compare_digest
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import APIError
from app.config import settings
from app.db.base import session_factory
from app.db.models import Service, User
from app.domain.access_policy import access_policy
from app.domain.maintenance import protect_mutation
from app.transport_security import redis_tls

shared_cache: Redis | None = None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def get_redis() -> AsyncIterator[Redis]:
    if shared_cache is not None:
        yield shared_cache
        return
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True, **redis_tls())
    try:
        yield client
    finally:
        await client.aclose()


DB = Annotated[AsyncSession, Depends(get_db)]
Cache = Annotated[Redis, Depends(get_redis)]


def decode_session(token: str | None, kind: str = "session") -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token or "",
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "jti", "kind", "csrf"]},
        )
        if payload["kind"] != kind:
            raise ValueError("wrong token kind")
        return payload
    except (jwt.PyJWTError, ValueError) as error:
        raise APIError(401, "UNAUTHENTICATED", "Сессия истекла. Войдите снова.") from error


def check_csrf(request: Request, payload: dict[str, Any]) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    if not token or not compare_digest(token, str(payload["csrf"])):
        raise APIError(403, "FORBIDDEN", "Защитный токен не совпадает. Обновите страницу.")
    origin = request.headers.get("origin")
    if origin and origin not in settings.cors_origins:
        raise APIError(403, "FORBIDDEN", "Источник запроса не разрешён.")


async def current_user(request: Request, db: DB, cache: Cache) -> User:
    payload = decode_session(request.cookies.get("session"))
    policy = await access_policy(db)
    if datetime.now(UTC).timestamp() >= float(payload["iat"]) + policy.session_minutes * 60:
        raise APIError(401, "UNAUTHENTICATED", "Срок сессии завершён. Войдите снова.")
    try:
        user_id = UUID(payload["sub"])
    except (KeyError, TypeError, ValueError) as error:
        raise APIError(401, "UNAUTHENTICATED", "Недействительная сессия.") from error
    revoked, revoked_before = await cache.mget(
        "revoked:" + payload["jti"], f"user_sessions_before:{user_id}"
    )
    if revoked:
        raise APIError(401, "UNAUTHENTICATED", "Сессия завершена.")
    if revoked_before and float(payload["iat"]) < float(revoked_before):
        raise APIError(401, "UNAUTHENTICATED", "Учётные данные изменены. Войдите снова.")
    row = (
        (
            await db.execute(
                select(User, Service)
                .outerjoin(Service, Service.id == User.service_id)
                .where(User.id == user_id)
            )
        )
        .tuples()
        .first()
    )
    if row is None or not row[0].is_active:
        raise APIError(401, "UNAUTHENTICATED", "Пользователь недоступен.")
    user, service = row
    if user.role == "ADMIN" and policy.require_admin_totp and not user.totp_secret:
        raise APIError(401, "UNAUTHENTICATED", "Требуется двухфакторная защита администратора.")
    # Hold the eager service in the request's identity map for card/status rendering.
    db.info["authenticated_service"] = service
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_csrf(request, payload)
    request.state.user_id, request.state.session = str(user.id), payload
    return user


def require_role(*roles: str) -> Callable[..., Coroutine[Any, Any, User]]:
    async def allowed(
        user: Annotated[User, Depends(current_user)], request: Request, db: DB
    ) -> User:
        if user.role not in roles:
            raise APIError(403, "FORBIDDEN", "Недостаточно прав для этого действия.")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not (
            user.role == "ADMIN"
            and request.url.path.startswith(
                ("/api/v1/admin/maintenance", "/api/v1/admin/operations/")
            )
        ):
            await protect_mutation(db)
        return user

    return allowed


Student = Annotated[User, Depends(require_role("STUDENT"))]
TrainingUser = Annotated[User, Depends(require_role("STUDENT", "TEACHER"))]
Teacher = Annotated[User, Depends(require_role("TEACHER"))]
Admin = Annotated[User, Depends(require_role("ADMIN"))]
