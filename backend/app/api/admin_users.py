import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID
from xml.etree.ElementTree import Element, tostring

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, Admin, Cache
from app.api.errors import APIError
from app.db.models import AuditLog, Lesson, LessonParticipant, Service, User, Workstation
from app.domain.access_policy import access_policy, validate_password_policy
from app.domain.exports import xml_value
from app.domain.pagination import next_cursor, page_offset
from app.domain.security import password_hasher

router = APIRouter(prefix="/admin", tags=["admin"])


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    login: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    last_name: str = Field(min_length=1, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    middle_name: str | None = Field(default=None, max_length=100)
    role: Literal["ADMIN", "TEACHER", "STUDENT"]
    service_id: UUID | None = None
    password: str = Field(min_length=8, max_length=256)
    totp_secret: str | None = Field(
        default=None, min_length=16, max_length=64, pattern=r"^[A-Z2-7]+$"
    )


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    login: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    middle_name: str | None = Field(default=None, max_length=100)
    role: Literal["ADMIN", "TEACHER", "STUDENT"] | None = None
    service_id: UUID | None = None
    is_active: bool | None = None
    totp_secret: str | None = Field(
        default=None, min_length=16, max_length=64, pattern=r"^[A-Z2-7]+$"
    )


class PasswordInput(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class WorkstationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: str = Field(min_length=1, max_length=16)
    room: str | None = Field(default=None, max_length=64)
    is_active: bool = True


def public_user(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "login": user.login,
        "last_name": user.last_name,
        "first_name": user.first_name,
        "middle_name": user.middle_name,
        "role": user.role,
        "service_id": str(user.service_id) if user.service_id else None,
        "is_active": user.is_active,
        "totp_enabled": bool(user.totp_secret),
    }


async def validate_service(db: DB, role: str, service_id: UUID | None) -> None:
    if role == "STUDENT" and service_id is None:
        raise APIError(400, "VALIDATION_ERROR", "Обучающемуся необходимо назначить службу.")
    if service_id and await db.get(Service, service_id) is None:
        raise APIError(400, "VALIDATION_ERROR", "Служба не найдена.")


async def commit_unique(db: DB) -> None:
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise APIError(
            400, "VALIDATION_ERROR", "Такой логин или номер рабочего места уже существует."
        ) from error


@router.get("/users")
async def users(
    db: DB, user: Admin, limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: str | None = None
) -> dict[str, Any]:
    offset = page_offset(cursor)
    rows = list(await db.scalars(select(User).order_by(User.login).offset(offset).limit(limit + 1)))
    return {
        "items": [public_user(row) for row in rows[:limit]],
        "next_cursor": next_cursor(offset, limit, len(rows) > limit),
    }


@router.get("/services")
async def services(db: DB, user: Admin) -> dict[str, Any]:
    return {
        "items": [
            {"id": str(row.id), "name": row.name}
            for row in await db.scalars(select(Service).order_by(Service.sort_order, Service.name))
        ]
    }


@router.post("/users", status_code=201)
async def create_user(body: UserInput, db: DB, user: Admin) -> dict[str, Any]:
    await validate_password_policy(db, body.password)
    if body.role == "ADMIN":
        await db.scalars(
            select(User)
            .where(User.role == "ADMIN", User.is_active)
            .order_by(User.id)
            .with_for_update()
        )
        if (await access_policy(db)).require_admin_totp and not body.totp_secret:
            raise APIError(
                400, "ADMIN_TOTP_REQUIRED", "Администратору необходима двухфакторная защита."
            )
    await validate_service(db, body.role, body.service_id)
    row = User(
        **body.model_dump(exclude={"password", "login"}),
        login=body.login.lower(),
        password_hash=await asyncio.to_thread(password_hasher.hash, body.password),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise APIError(400, "VALIDATION_ERROR", "Такой логин уже существует.") from error
    db.add(
        AuditLog(
            user_id=user.id,
            action="USER_CREATED",
            entity_type="users",
            entity_id=row.id,
            payload=public_user(row),
        )
    )
    await db.commit()
    return public_user(row)


@router.patch("/users")
async def update_user(body: UserPatch, db: DB, cache: Cache, user: Admin) -> dict[str, Any]:
    admins = list(
        await db.scalars(
            select(User)
            .where(User.role == "ADMIN", User.is_active)
            .order_by(User.id)
            .with_for_update()
        )
    )
    row = await db.get(User, body.id)
    if row is None:
        raise APIError(404, "NOT_FOUND", "Пользователь не найден.")
    changes = body.model_dump(exclude_unset=True, exclude={"id"})
    if any(
        changes.get(key, True) is None
        for key in ("login", "last_name", "first_name", "role", "is_active")
    ):
        raise APIError(400, "VALIDATION_ERROR", "Обязательные поля нельзя очистить.")
    role, active = changes.get("role", row.role), changes.get("is_active", row.is_active)
    if (
        active
        and role == "ADMIN"
        and (await access_policy(db)).require_admin_totp
        and not changes.get("totp_secret", row.totp_secret)
    ):
        raise APIError(
            400, "ADMIN_TOTP_REQUIRED", "Нельзя отключить обязательную двухфакторную защиту."
        )
    if row in admins and len(admins) == 1 and (role != "ADMIN" or not active):
        raise APIError(400, "VALIDATION_ERROR", "Нельзя отключить последнего администратора.")
    service_id = changes.get("service_id", row.service_id)
    await validate_service(db, role, service_id)
    if role != row.role or service_id != row.service_id:
        running = await db.scalar(
            select(LessonParticipant.id)
            .join(Lesson)
            .where(LessonParticipant.student_id == row.id, Lesson.status == "RUNNING")
        )
        if running:
            raise APIError(
                409, "VALIDATION_ERROR", "Сначала завершите активное занятие пользователя."
            )
    previous = public_user(row)
    for name, value in changes.items():
        setattr(row, name, value.lower() if name == "login" else value)
    db.add(
        AuditLog(
            user_id=user.id,
            action="USER_UPDATED",
            entity_type="users",
            entity_id=row.id,
            payload={"previous": previous, "current": public_user(row)},
        )
    )
    await commit_unique(db)
    if "totp_secret" in changes or not active or previous["role"] != role:
        await cache.set(
            f"user_sessions_before:{row.id}", str(datetime.now(UTC).timestamp()), ex=28800
        )
    return public_user(row)


@router.post("/users/{user_id}/block")
async def block(user_id: UUID, db: DB, cache: Cache, user: Admin) -> dict[str, Any]:
    return await update_user(UserPatch(id=user_id, is_active=False), db, cache, user)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: UUID, body: PasswordInput, db: DB, cache: Cache, user: Admin
) -> dict[str, bool]:
    await validate_password_policy(db, body.password)
    row = await db.get(User, user_id)
    if row is None:
        raise APIError(404, "NOT_FOUND", "Пользователь не найден.")
    row.password_hash = await asyncio.to_thread(password_hasher.hash, body.password)
    db.add(
        AuditLog(user_id=user.id, action="PASSWORD_RESET", entity_type="users", entity_id=row.id)
    )
    await db.commit()
    await cache.set(f"user_sessions_before:{row.id}", str(datetime.now(UTC).timestamp()), ex=28800)
    return {"ok": True}


@router.get("/workstations")
async def workstations(db: DB, user: Admin) -> dict[str, Any]:
    return {
        "items": [
            {"id": str(row.id), "number": row.number, "room": row.room, "is_active": row.is_active}
            for row in await db.scalars(select(Workstation).order_by(Workstation.number))
        ],
        "next_cursor": None,
    }


@router.post("/workstations", status_code=201)
async def create_workstation(body: WorkstationInput, db: DB, user: Admin) -> dict[str, Any]:
    row = Workstation(**body.model_dump())
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise APIError(400, "VALIDATION_ERROR", "Номер рабочего места уже существует.") from error
    db.add(
        AuditLog(
            user_id=user.id,
            action="WORKSTATION_CREATED",
            entity_type="workstations",
            entity_id=row.id,
            payload=body.model_dump(),
        )
    )
    await db.commit()
    return {"id": str(row.id), **body.model_dump()}


@router.get("/workstations.xml")
async def workstations_xml(db: DB, user: Admin) -> Response:
    root = Element("workstations", {"schema": "1"})
    data = await workstations(db, user)
    xml_value(root, data)
    db.add(
        AuditLog(
            user_id=user.id,
            action="WORKSTATIONS_EXPORTED",
            entity_type="workstations",
            payload={"format": "XML", "count": len(data["items"])},
        )
    )
    await db.commit()
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
        headers={
            "Content-Disposition": 'attachment; filename="workstations.xml"',
            "Cache-Control": "no-store",
        },
    )
