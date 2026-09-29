"""Interactive boss call: the student speaks (or types) turns, the duty officer answers from the graph."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DB, Student, Teacher
from app.api.errors import APIError
from app.config import settings
from app.db.models import AuditLog, DirectoryEntry, InteractionEvent, PhoneReport, Scenario
from app.dialogue import learning
from app.dialogue.engine import Card, Turn, load_graph, step
from app.domain.cards import own_assignment
from app.realtime.hub import hub

router = APIRouter(tags=["dialogue"])
GRAPH_PATH = Path("/data/dialogue/boss_graph.json")
MEDIA_ROOT = Path("/data/voices")


def graph() -> dict[str, Any]:
    # Small file, read per turn so teacher-approved changes apply to the very next call.
    return load_graph(GRAPH_PATH)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    call_id: UUID
    utterance: str = Field(default="", max_length=2000)


def audio_url(voice: str, node: str) -> str | None:
    # Lines are rendered offline per voice (scripts/build_dialogue_voices.py); text is the fallback.
    return f"/media/voices/{voice}/dialogue/{node}.wav" if (MEDIA_ROOT / voice / "dialogue" / f"{node}.wav").is_file() else None


@router.post("/student/assignments/{assignment_id}/dialogue")
async def dialogue_turn(assignment_id: UUID, body: TurnInput, db: DB, user: Student) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    if assignment.state == "CLOSED" or lesson.status != "RUNNING":
        raise APIError(409, "CARD_CLOSED", "Карточка закрыта или занятие завершено.")
    call = await db.scalar(
        select(PhoneReport).where(PhoneReport.id == body.call_id, PhoneReport.assignment_id == assignment.id)
    )
    if call is None:
        raise APIError(404, "NOT_FOUND", "Вызов не найден в этой карточке.")
    if call.transcript is not None:
        raise APIError(409, "VALIDATION_ERROR", "Разговор уже завершён.")
    entry = (await db.scalars(select(DirectoryEntry).where(DirectoryEntry.code == call.callee_code))).one()
    scenario = await db.get(Scenario, assignment.scenario_id)
    assert scenario is not None
    payload = scenario.card_payload
    card = Card(address=str(payload.get("address", {}).get("raw", "")),
                incident_type=str(payload.get("incident_type_name", "")))
    history = list(
        await db.scalars(
            select(InteractionEvent)
            .where(InteractionEvent.assignment_id == assignment.id, InteractionEvent.kind == "DIALOGUE_TURN")
            .order_by(InteractionEvent.created_at)
        )
    )
    turns = [Turn(e.payload["utterance"], e.payload["asked"]) for e in history
             if e.payload and e.payload.get("call_id") == str(call.id)]
    now = datetime.now(UTC)
    if body.utterance:
        asked = step(graph(), card, turns).node
        turns.append(Turn(body.utterance, asked))
    reply = step(graph(), card, turns)
    if body.utterance:
        db.add(InteractionEvent(assignment_id=assignment.id, kind="DIALOGUE_TURN", created_at=now, payload={
            "call_id": str(call.id), "utterance": body.utterance, "asked": turns[-1].asked,
            "reply": reply.node, "filled": reply.filled,
        }))
        if reply.unmatched:
            # Raw material for the overnight job; nothing is learned until the teacher approves.
            db.add(InteractionEvent(assignment_id=assignment.id, kind="DIALOGUE_UNMATCHED", created_at=now,
                                    payload={"utterance": body.utterance, "asked": turns[-1].asked}))
    if reply.final:
        call.transcript = "\n".join(t.utterance for t in turns)
        call.duration_ms = max(0, round((now - call.created_at).total_seconds() * 1000))
        report = {"call_id": str(call.id), "callee_code": call.callee_code, "transcript": call.transcript,
                  "duration_ms": call.duration_ms, "dialogue": True, "missing": reply.extra.get("missing", [])}
        db.add(InteractionEvent(assignment_id=assignment.id, kind="PHONE_REPORT", payload=report, created_at=now))
        db.add(AuditLog(user_id=user.id, action="PHONE_REPORT", entity_type="assignment",
                        entity_id=assignment.id, payload=report, created_at=now))
    await db.commit()
    if reply.final:
        await hub.send(f"user:{lesson.teacher_id}", "STUDENT_ACTION", {
            "student_id": str(user.id), "assignment_id": str(assignment.id), "kind": "PHONE_REPORT", "status": None,
        }, now)
    return {
        "node": reply.node,
        "text": reply.text,
        "audio_url": audio_url(entry.voice, reply.node),
        "filled": reply.filled,
        "final": reply.final,
        "unmatched": reply.unmatched,
        "extra": reply.extra,
    }


@router.get("/teacher/dialogue/unmatched")
async def unmatched(db: DB, user: Teacher) -> dict[str, Any]:
    """What students said that the graph did not understand, most frequent first."""
    rows = list(await db.scalars(select(InteractionEvent).where(InteractionEvent.kind == "DIALOGUE_UNMATCHED")))
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.payload:
            key = (row.payload["utterance"].lower().strip(" .!?"), row.payload["asked"])
            counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda item: -item[1])
    return {"items": [{"utterance": u, "asked": a, "count": c} for (u, a), c in items]}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    approve: bool
    slot: str | None = Field(default=None, pattern="^(address|incident_type|victims|measures|none)$")
    reply: str | None = Field(default=None, max_length=120)


@router.get("/teacher/dialogue/proposals")
async def proposals(user: Teacher) -> dict[str, Any]:
    items = learning.read(learning.PROPOSALS, [])
    return {"graph_version": load_graph(GRAPH_PATH).get("version", 1),
            "items": sorted(items, key=lambda p: (p["status"] != "PENDING", -p.get("count", 0)))}


@router.post("/teacher/dialogue/proposals/{proposal_id}")
async def decide(proposal_id: str, body: Decision, db: DB, user: Teacher) -> dict[str, Any]:
    try:
        result = learning.decide(proposal_id, body.approve, body.slot, body.reply, user.login)
    except KeyError as error:
        raise APIError(404, "NOT_FOUND", "Предложение не найдено или уже рассмотрено.") from error
    db.add(AuditLog(user_id=user.id, action="DIALOGUE_PROPOSAL_DECIDED", entity_type="dialogue",
                    payload={"proposal": proposal_id, "approve": body.approve, "slot": result["slot"]}))
    await db.commit()
    return result


@router.post("/student/assignments/{assignment_id}/dialogue/transcribe")
async def transcribe(assignment_id: UUID, request: Request, db: DB, user: Student) -> dict[str, Any]:
    """Speech to text for one phrase of the boss call: 16 kHz mono 16-bit PCM in, text out (offline Vosk)."""
    await own_assignment(db, assignment_id, user.id)
    audio = await request.body()
    if not 0 < len(audio) <= 60 * 16000 * 2:
        raise APIError(413, "VALIDATION_ERROR", "Фраза должна быть короче минуты.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{settings.stt_url.rstrip('/')}/transcribe", content=audio)
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise APIError(503, "STT_UNAVAILABLE", "Распознавание речи недоступно. Введите ответ текстом.") from error
    return {"text": str(response.json().get("text", ""))}
