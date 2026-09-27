import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError
from sqlalchemy import select

from app.api.deps import DB, Cache, decode_session
from app.api.errors import APIError
from app.config import settings
from app.db.models import Lesson, LessonParticipant, User, Workstation
from app.domain.access_policy import access_policy
from app.realtime.hub import hub

router = APIRouter()


@router.websocket("/ws")
async def websocket(socket: WebSocket, db: DB, cache: Cache, role: str) -> None:
    try:
        claims = decode_session(socket.cookies.get("session"))
        revoked_before = await cache.get(f"user_sessions_before:{claims['sub']}")
        user = await db.get(User, UUID(claims["sub"]))
        if (
            not user
            or not user.is_active
            or role not in {"student", "teacher"}
            or user.role not in ({"STUDENT"} if role == "student" else {"TEACHER"})
            or socket.headers.get("origin") not in settings.cors_origins
            or await cache.exists("revoked:" + claims["jti"])
            or (revoked_before is not None and float(claims["iat"]) < float(revoked_before))
        ):
            await socket.close(code=1008)
            return
    except (APIError, KeyError, ValueError, TypeError):
        await socket.close(code=1008)
        return
    await socket.accept()
    user_id = UUID(claims["sub"])
    already_online = await hub.is_online(user_id)
    hub.join(socket, f"user:{user_id}")
    memberships: dict[UUID, tuple[UUID, str | None]] = {}
    await db.rollback()
    try:
        while True:
            await hub.touch(socket, user_id)
            now = datetime.now(UTC)
            policy = await access_policy(db)
            revoked_before = await cache.get(f"user_sessions_before:{claims['sub']}")
            if (
                now.timestamp() >= claims["exp"]
                or now.timestamp() >= float(claims["iat"]) + policy.session_minutes * 60
                or await cache.exists("revoked:" + claims["jti"])
                or (revoked_before is not None and float(claims["iat"]) < float(revoked_before))
            ):
                await socket.close(code=1008)
                return
            current = (
                await db.execute(
                    select(User.is_active, User.role).where(User.id == UUID(claims["sub"]))
                )
            ).first()
            allowed_roles = {"STUDENT"} if role == "student" else {"TEACHER"}
            if not current or not current.is_active or current.role not in allowed_roles:
                await socket.close(code=1008)
                return
            query = select(Lesson).where(Lesson.status == "RUNNING")
            if role == "teacher":
                query = query.where(Lesson.teacher_id == UUID(claims["sub"]))
            else:
                query = query.join(LessonParticipant).where(
                    LessonParticipant.student_id == UUID(claims["sub"])
                )
            lessons = list(await db.scalars(query))
            for lesson in lessons:
                hub.join(socket, f"{role}:{lesson.id}")
                if role == "student" and lesson.id not in memberships:
                    station = await db.scalar(
                        select(Workstation.number)
                        .join(LessonParticipant, LessonParticipant.workstation_id == Workstation.id)
                        .where(
                            LessonParticipant.lesson_id == lesson.id,
                            LessonParticipant.student_id == UUID(claims["sub"]),
                        )
                    )
                    memberships[lesson.id] = (lesson.teacher_id, station)
                    if not already_online:
                        await hub.send(
                            f"user:{lesson.teacher_id}",
                            "STUDENT_JOINED",
                            {
                                "student_id": claims["sub"],
                                "workstation": station,
                            },
                            now,
                        )
            await db.rollback()
            async with hub.locks[socket]:
                await socket.send_json(
                    {
                        "type": "HEARTBEAT",
                        "ts": now.isoformat(),
                        "payload": {"server_time": now.isoformat()},
                    }
                )
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=5)
            except TimeoutError:
                continue
    except (WebSocketDisconnect, RuntimeError, OSError, KeyError, RedisError):
        hub.leave(socket)
    finally:
        await hub.detach(socket)
        if role == "student" and not await hub.is_online(UUID(claims["sub"])):
            for teacher_id, station in memberships.values():
                await hub.send(
                    f"user:{teacher_id}",
                    "STUDENT_LEFT",
                    {
                        "student_id": claims["sub"],
                        "workstation": station,
                    },
                )
