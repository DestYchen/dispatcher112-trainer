from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DB, Student
from app.api.errors import APIError
from app.db.models import AuditLog, DirectoryEntry, InteractionEvent, PhoneReport, SipCall
from app.domain.cards import own_assignment
from app.domain.idempotency import previous_response, save_response
from app.realtime.hub import hub

router = APIRouter(prefix="/student", tags=["phone"])


class CallInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: str = Field(min_length=1, max_length=16, pattern=r"^[0-9*#]+$")


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    call_id: UUID
    transcript: str = Field(min_length=1, max_length=10000)
    duration_ms: int = Field(ge=0, le=86400000)


@router.get("/directory")
async def directory(db: DB, user: Student) -> dict[str, Any]:
    rows = list(await db.scalars(select(DirectoryEntry).order_by(DirectoryEntry.number)))
    return {
        "entries": [
            {
                "code": row.code,
                "number": row.number,
                "title": row.title,
                "greeting_audio_url": f"/media/voices/{row.voice}/greeting.wav",
            }
            for row in rows
        ]
    }


@router.post("/assignments/{assignment_id}/call")
async def call(assignment_id: UUID, body: CallInput, db: DB, user: Student) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    if assignment.state == "CLOSED":
        raise APIError(409, "CARD_CLOSED", "Карточка закрыта для редактирования.")
    if lesson.status != "RUNNING" or assignment.opened_at is None:
        raise APIError(409, "LESSON_NOT_RUNNING", "Откройте карточку в текущем занятии.")
    entry = await db.scalar(select(DirectoryEntry).where(DirectoryEntry.number == body.number))
    if entry is None:
        raise APIError(
            404, "NOT_FOUND", f"Номер {body.number} не отвечает. Проверьте номер по справочнику."
        )
    now = datetime.now(UTC)
    report = PhoneReport(
        assignment_id=assignment.id,
        callee_code=entry.code,
        dialed_number=entry.number,
        created_at=now,
    )
    db.add(report)
    await db.flush()
    payload = {"call_id": str(report.id), "callee_code": entry.code, "number": entry.number}
    db.add(
        InteractionEvent(
            assignment_id=assignment.id, kind="CALL_STARTED", payload=payload, created_at=now
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="CALL_STARTED",
            entity_type="assignment",
            entity_id=assignment.id,
            payload=payload,
            created_at=now,
        )
    )
    await db.commit()
    await hub.send(
        f"user:{lesson.teacher_id}",
        "STUDENT_ACTION",
        {
            "student_id": str(user.id),
            "assignment_id": str(assignment.id),
            "kind": "CALL_STARTED",
            "status": None,
        },
        now,
    )
    return {
        "call_id": str(report.id),
        "callee": {"code": entry.code, "title": entry.title},
        "started_at": now.isoformat(),
        "greeting_audio_url": f"/media/voices/{entry.voice}/greeting.wav",
        "greeting_text": entry.greeting,
    }


@router.post("/assignments/{assignment_id}/report")
async def report(
    assignment_id: UUID,
    body: ReportInput,
    db: DB,
    user: Student,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    now = datetime.now(UTC)
    original = body.model_dump(mode="json")
    previous = await previous_response(
        db, user.id, assignment.id, "report", idempotency_key, original, now
    )
    if previous is not None:
        return previous
    if assignment.state == "CLOSED":
        raise APIError(409, "CARD_CLOSED", "Карточка закрыта для редактирования.")
    if lesson.status != "RUNNING":
        raise APIError(409, "LESSON_NOT_RUNNING", "Занятие завершено.")
    row = await db.scalar(
        select(PhoneReport)
        .where(PhoneReport.id == body.call_id, PhoneReport.assignment_id == assignment.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Вызов не найден в этой карточке.")
    if row.transcript is not None:
        raise APIError(409, "VALIDATION_ERROR", "Доклад по этому вызову уже сохранён.")
    entry = (
        await db.scalars(select(DirectoryEntry).where(DirectoryEntry.code == row.callee_code))
    ).one()
    sip_call = await db.get(SipCall, row.id)
    if sip_call and sip_call.answered_at is None:
        raise APIError(409, "CALL_NOT_ANSWERED", "Доклад возможен после соединения с абонентом.")
    row.transcript = body.transcript
    # Duration in the browser is informational; the stored interval uses server timestamps.
    row.duration_ms = max(0, round((now - row.created_at).total_seconds() * 1000))
    if sip_call:
        assert sip_call.answered_at is not None
        row.duration_ms = max(
            0, round(((sip_call.ended_at or now) - sip_call.answered_at).total_seconds() * 1000)
        )
    payload = {
        "call_id": str(row.id),
        "callee_code": row.callee_code,
        "transcript": row.transcript,
        "duration_ms": row.duration_ms,
    }
    db.add(
        InteractionEvent(
            assignment_id=assignment.id, kind="PHONE_REPORT", payload=payload, created_at=now
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="PHONE_REPORT",
            entity_type="assignment",
            entity_id=assignment.id,
            payload=payload,
            created_at=now,
        )
    )
    result = {
        "confirmation_audio_url": f"/media/voices/{entry.voice}/confirm.wav",
        "confirmation_text": entry.confirmation,
    }
    save_response(db, user.id, assignment.id, "report", idempotency_key, original, result, now)
    await db.commit()
    await hub.send(
        f"user:{lesson.teacher_id}",
        "STUDENT_ACTION",
        {
            "student_id": str(user.id),
            "assignment_id": str(assignment.id),
            "kind": "PHONE_REPORT",
            "status": None,
        },
        now,
    )
    return result
