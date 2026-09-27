import asyncio
import wave
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.api.deps import DB, Student, TrainingUser
from app.api.errors import APIError
from app.config import settings
from app.db.models import (
    Assignment,
    AuditLog,
    DirectoryEntry,
    Lesson,
    PhoneReport,
    Scenario,
    SipCall,
    User,
)
from app.domain.card_entry import incoming_message
from app.domain.cards import own_assignment
from app.domain.idempotency import previous_response, save_response
from app.domain.sip import (
    ACTIVE_STATES,
    ROOT,
    control_password,
    endpoint_name,
    endpoint_password,
    speech_name,
)
from app.transport_security import http_verify

router = APIRouter(prefix="/sip", tags=["sip"])
encoding_slots = asyncio.Semaphore(2)


class SipCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["INBOUND", "OUTBOUND"]
    number: str | None = Field(default=None, pattern=r"^[0-9*#]{1,16}$")


def call_payload(row: SipCall) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "assignment_id": str(row.assignment_id),
        "direction": row.direction,
        "state": row.state,
        "callee_code": row.callee_code,
        "dial_uri": f"sip:{row.id.hex}@dispatcher112" if row.direction == "OUTBOUND" else None,
        "created_at": row.created_at.isoformat(),
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "failure_reason": row.failure_reason,
        "recording_available": row.ended_at is not None
        and (ROOT / "recordings" / f"{row.id.hex}.wav").is_file(),
    }


@router.get("/session")
async def sip_session(response: Response, db: DB, user: TrainingUser) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    active = await db.scalar(
        select(SipCall).where(SipCall.user_id == user.id, SipCall.state.in_(ACTIVE_STATES))
    )
    return {
        "uri": f"sip:{endpoint_name(user.id)}@dispatcher112",
        "username": endpoint_name(user.id),
        "password": endpoint_password(settings.jwt_secret, user.id, user.password_hash),
        "websocket_path": "/sip",
        "echo_uri": "sip:9000@dispatcher112",
        "active_call": call_payload(active) if active else None,
    }


@router.post("/assignments/{assignment_id}/calls")
async def create_call(
    assignment_id: UUID,
    body: SipCallInput,
    db: DB,
    user: Student,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    now = datetime.now(UTC)
    original = body.model_dump(mode="json")
    previous = await previous_response(
        db, user.id, assignment.id, "sip-call", idempotency_key, original, now
    )
    if previous is not None:
        return previous
    if assignment.state == "CLOSED":
        raise APIError(409, "CARD_CLOSED", "Карточка уже закрыта.")
    if lesson.status != "RUNNING":
        raise APIError(409, "LESSON_NOT_RUNNING", "Учебный вызов доступен в текущем занятии.")
    active = await db.scalar(
        select(SipCall.id).where(SipCall.user_id == user.id, SipCall.state.in_(ACTIVE_STATES))
    )
    if active:
        raise APIError(409, "PHONE_BUSY", "Завершите текущий вызов.")
    callee = None
    if body.direction == "INBOUND":
        if assignment.task_mode != "CARD_ENTRY" or body.number is not None:
            raise APIError(
                400, "VALIDATION_ERROR", "Входящий вызов доступен для заполнения карточки."
            )
        scenario = await db.get(Scenario, assignment.scenario_id)
        assert scenario is not None
        message = incoming_message(scenario.card_payload)
    else:
        if assignment.opened_at is None:
            raise APIError(409, "INVALID_TRANSITION", "Сначала примите карточку.")
        callee = await db.scalar(select(DirectoryEntry).where(DirectoryEntry.number == body.number))
        if callee is None:
            raise APIError(404, "NOT_FOUND", "Разрешены только номера учебного справочника.")
        message = callee.greeting
    media_name = speech_name(message)
    if not (ROOT / "speech" / f"{media_name}.wav").is_file():
        raise APIError(
            409, "VOICE_NOT_READY", "Реплика ещё готовится. Повторите через несколько секунд."
        )
    row = SipCall(
        user_id=user.id,
        assignment_id=assignment.id,
        direction=body.direction,
        media_name=media_name,
        callee_code=callee.code if callee else None,
        created_at=now,
    )
    db.add(row)
    await db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            action="SIP_CALL_REQUESTED",
            entity_type="sip_call",
            entity_id=row.id,
            payload=original,
            created_at=now,
        )
    )
    result = call_payload(row)
    if callee is not None:
        db.add(
            PhoneReport(
                id=row.id,
                assignment_id=assignment.id,
                callee_code=callee.code,
                dialed_number=callee.number,
                created_at=now,
            )
        )
        result.update(
            call_id=str(row.id),
            started_at=now.isoformat(),
            callee={"code": callee.code, "title": callee.title},
            greeting_audio_url=f"/media/voices/{callee.voice}/greeting.wav",
            greeting_text=callee.greeting,
        )
    save_response(db, user.id, assignment.id, "sip-call", idempotency_key, original, result, now)
    await db.commit()
    return result


@router.get("/assignments/{assignment_id}/calls")
async def calls(assignment_id: UUID, db: DB, user: Student) -> dict[str, Any]:
    await own_assignment(db, assignment_id, user.id)
    rows = await db.scalars(
        select(SipCall)
        .where(SipCall.assignment_id == assignment_id)
        .order_by(SipCall.created_at.desc())
        .limit(100)
    )
    return {"calls": [call_payload(row) for row in rows]}


@router.get("/calls/{call_id}/recording")
async def recording(
    call_id: UUID, db: DB, user: TrainingUser, format: Literal["wav", "mp3"] = "wav"
) -> FileResponse:
    row = await db.get(SipCall, call_id)
    if row is None:
        raise APIError(404, "NOT_FOUND", "Запись не найдена.")
    assignment = await db.get(Assignment, row.assignment_id)
    assert assignment is not None
    lesson = await db.get(Lesson, assignment.lesson_id)
    if row.user_id != user.id and not (
        lesson and user.role == "TEACHER" and lesson.teacher_id == user.id
    ):
        raise APIError(404, "NOT_FOUND", "Запись не найдена.")
    path = ROOT / "recordings" / f"{row.id.hex}.wav"
    if row.ended_at is None or not path.is_file():
        raise APIError(404, "NOT_FOUND", "Запись ещё недоступна.")
    if format == "mp3":
        directory = TemporaryDirectory(prefix="dispatcher-recording-")
        target = Path(directory.name) / f"{call_id}.mp3"
        try:
            # LAME also accepts raw PCM; reject a damaged WAV instead of treating it as audio.
            with wave.open(str(path), "rb") as source:
                if source.getnframes() == 0:
                    raise ValueError("Empty audio")
            async with encoding_slots:
                process = await asyncio.create_subprocess_exec(
                    "lame",
                    "--silent",
                    "--noreplaygain",
                    "-b",
                    "64",
                    str(path),
                    str(target),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    async with asyncio.timeout(60):
                        returncode = await process.wait()
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    raise
                if returncode != 0 or not target.is_file() or target.stat().st_size == 0:
                    raise ValueError("Audio encoding failed")
            return FileResponse(
                target,
                media_type="audio/mpeg",
                filename=f"call-{call_id}.mp3",
                headers={"Cache-Control": "private, no-store"},
                background=BackgroundTask(directory.cleanup),
            )
        except (OSError, ValueError, TimeoutError, wave.Error, EOFError) as error:
            directory.cleanup()
            raise APIError(
                503, "PHONE_UNAVAILABLE", "Не удалось подготовить запись MP3."
            ) from error
        except BaseException:
            directory.cleanup()
            raise
    return FileResponse(
        path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"}
    )


@router.get("/calls/{call_id}")
async def call_state(call_id: UUID, db: DB, user: Student) -> dict[str, Any]:
    row = await db.scalar(select(SipCall).where(SipCall.id == call_id, SipCall.user_id == user.id))
    if row is None:
        raise APIError(404, "NOT_FOUND", "Учебный вызов не найден.")
    return call_payload(row)


@router.post("/calls/{call_id}/cancel")
async def cancel_call(call_id: UUID, db: DB, user: Student) -> dict[str, Any]:
    row = await db.scalar(
        select(SipCall).where(SipCall.id == call_id, SipCall.user_id == user.id).with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Учебный вызов не найден.")
    if row.answered_at:
        raise APIError(409, "INVALID_TRANSITION", "Завершите принятый звонок на панели телефона.")
    if row.state not in ACTIVE_STATES:
        return call_payload(row)
    if row.channel_id:
        try:
            async with httpx.AsyncClient(
                auth=("dispatcher", control_password(settings.jwt_secret)),
                timeout=3,
                trust_env=False,
                verify=http_verify(),
            ) as client:
                response = await client.delete(
                    f"{settings.sip_ari_url.rstrip('/')}/channels/{row.channel_id}"
                )
                if response.status_code != 404:
                    response.raise_for_status()
        except httpx.HTTPError as error:
            raise APIError(
                503,
                "PHONE_UNAVAILABLE",
                "АТС недоступна. Повторите отмену после восстановления связи.",
            ) from error
    row.state, row.ended_at = "FAILED", datetime.now(UTC)
    row.failure_reason = "Неотвеченный вызов отменён студентом."
    db.add(
        AuditLog(
            user_id=user.id,
            action="SIP_CALL_CANCELLED",
            entity_type="sip_call",
            entity_id=row.id,
            created_at=row.ended_at,
        )
    )
    await db.commit()
    return call_payload(row)
