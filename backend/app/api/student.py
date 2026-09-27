from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DB, Student
from app.api.errors import APIError
from app.db.models import (
    Assignment,
    AuditLog,
    IncidentType,
    InteractionEvent,
    Lesson,
    LessonParticipant,
    Scenario,
    Service,
    StatusEvent,
    Workstation,
)
from app.domain.cards import (
    block_from_events,
    card_summaries,
    full_card,
    lesson_payload,
    own_assignment,
    timers,
)
from app.domain.enums import ResponseStatus
from app.domain.idempotency import previous_response, save_response
from app.domain.status_machine import (
    CommentRequired,
    InvalidTransition,
    is_terminal,
    validate_transition,
)
from app.realtime.hub import hub
from app.scoring.effective import effective_score
from app.scoring.grammar import check_grammar
from app.scoring.rules.address import check_addresses
from app.scoring.service import score_assignment

router = APIRouter(prefix="/student", tags=["student"])


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ResponseStatus
    comment: str | None = Field(default=None, max_length=10000)


@router.post("/assignments/{assignment_id}/status")
async def change_status(
    assignment_id: UUID,
    body: StatusInput,
    db: DB,
    user: Student,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    now = datetime.now(UTC)
    original = body.model_dump(mode="json")
    previous = await previous_response(
        db, user.id, assignment.id, "status", idempotency_key, original, now
    )
    if previous is not None:
        return previous
    if assignment.state == "CLOSED":
        raise APIError(409, "CARD_CLOSED", "Карточка закрыта для редактирования.")
    if lesson.status != "RUNNING":
        raise APIError(409, "LESSON_NOT_RUNNING", "Занятие не идёт.")
    if assignment.task_mode == "CARD_ENTRY":
        raise APIError(409, "INVALID_TRANSITION", "В этом упражнении заполните и сдайте карточку.")
    if assignment.opened_at is None or body.status in {"ADDED", "RECEIVED"}:
        raise APIError(409, "INVALID_TRANSITION", "Откройте карточку и выберите доступный статус.")
    events = list(
        await db.scalars(
            select(StatusEvent)
            .where(StatusEvent.assignment_id == assignment.id)
            .order_by(StatusEvent.created_at, StatusEvent.id)
        )
    )
    service = await db.get(Service, user.service_id) if user.service_id else None
    block = block_from_events(assignment, service, events)
    comment = body.comment.strip() if body.comment is not None else None
    try:
        validate_transition(block["current_status"], body.status, comment)
    except InvalidTransition as error:
        raise APIError(
            409,
            "INVALID_TRANSITION",
            "Переход в выбранный статус недоступен.",
            {"from": block["current_status"], "to": body.status},
        ) from error
    except CommentRequired as error:
        raise APIError(
            422,
            "COMMENT_REQUIRED",
            "Укажите причину отказа и куда передана информация: не менее 15 символов.",
        ) from error
    assert assignment.delivered_at is not None
    elapsed = max(0, round((now - assignment.delivered_at).total_seconds() * 1000))
    event = StatusEvent(
        id=uuid4(),
        assignment_id=assignment.id,
        status=body.status,
        comment=comment,
        is_automatic=False,
        elapsed_ms=elapsed,
        created_at=now,
    )
    db.add(event)
    events.append(event)
    if assignment.primary_status_at is None:
        assignment.primary_status_at = now
    assignment.state = "PRIMARY_SET"
    if is_terminal(body.status):
        assignment.state, assignment.closed_at = "CLOSED", now
    db.add(
        InteractionEvent(
            assignment_id=assignment.id,
            kind="STATUS_CHANGED",
            payload={"status": body.status},
            created_at=now,
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action="STATUS_CHANGED",
            entity_type="assignment",
            entity_id=assignment.id,
            payload={"status": body.status, "comment": comment},
            created_at=now,
        )
    )
    if assignment.state == "CLOSED":
        await score_assignment(db, assignment, lesson, events)
    result = {
        "my_block": block_from_events(assignment, service, events),
        "timers": timers(assignment, lesson, now),
    }
    save_response(db, user.id, assignment.id, "status", idempotency_key, original, result, now)
    await db.commit()
    payload = {
        "student_id": str(user.id),
        "assignment_id": str(assignment.id),
        "kind": "STATUS_CHANGED",
        "status": body.status,
    }
    await hub.send(f"user:{lesson.teacher_id}", "STUDENT_ACTION", payload, now)
    await hub.send(
        f"user:{user.id}",
        "CARD_CLOSED" if assignment.state == "CLOSED" else "STUDENT_ACTION",
        payload,
        now,
    )
    return result


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=10000)
    field: Literal["comment", "address", "report", "description"] = "comment"


@router.post("/assignments/{assignment_id}/check-text")
async def check_text(assignment_id: UUID, body: TextInput, db: DB, user: Student) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id)
    scenario = await db.get(Scenario, assignment.scenario_id)
    assert scenario is not None
    addresses = await check_addresses(db, body.text, scenario.card_payload["address"]["raw"])
    if not lesson.settings["grammar_check_enabled"]:
        return {
            "issues": addresses,
            "grammar_available": False,
            "notice": "Проверка грамотности отключена преподавателем.",
        }
    grammar = await check_grammar(body.text, body.field)
    issues = addresses + [
        item
        for item in grammar.items
        if not any(
            item["offset"] < address["offset"] + address["length"]
            and address["offset"] < item["offset"] + item["length"]
            for address in addresses
        )
    ]
    return {"issues": issues, "grammar_available": grammar.available, "notice": grammar.reason}


@router.get("/results")
async def results(db: DB, user: Student, lesson_id: UUID | None = None) -> dict[str, Any]:
    query = (
        select(Lesson)
        .join(LessonParticipant)
        .where(LessonParticipant.student_id == user.id, Lesson.status == "FINISHED")
    )
    if lesson_id:
        query = query.where(Lesson.id == lesson_id)
    lesson = await db.scalar(query.order_by(Lesson.finished_at.desc()).limit(1))
    if lesson is None:
        if lesson_id:
            raise APIError(404, "NOT_FOUND", "Результаты занятия недоступны.")
        return {"lesson": None, "summary": None, "cards": []}
    rows = list(
        await db.scalars(
            select(Assignment)
            .where(
                Assignment.lesson_id == lesson.id,
                Assignment.student_id == user.id,
                Assignment.state != "QUEUED",
            )
            .order_by(Assignment.card_number)
        )
    )
    cards = []
    for row in rows:
        scenario = await db.get(Scenario, row.scenario_id)
        assert scenario is not None
        incident = await db.get(IncidentType, scenario.incident_type_id)
        assert incident is not None
        score = row.score
        # A lesson created before the scoring upgrade is evaluated from its original events.
        if score is None:
            score = await score_assignment(db, row, lesson)
        effective = effective_score(score, row.teacher_override)
        cards.append(
            {
                "assignment_id": str(row.id),
                "card_number": row.card_number,
                "incident_type_name": incident.name,
                "total": effective["total"],
                "effective_score": effective,
                "score": score,
                "teacher_override": row.teacher_override,
                "violations": score["violations"],
            }
        )
    axes = {}
    for axis in ("timeliness", "correctness", "completeness", "literacy"):
        values = [
            card["effective_score"]["axes"][axis]["score"]
            for card in cards
            if card["effective_score"]["axes"][axis]["score"] is not None
        ]
        axes[axis] = round(sum(values) / len(values), 2) if values else None
    await db.commit()
    return {
        "lesson": {
            "id": str(lesson.id),
            "title": lesson.title,
            "finished_at": lesson.finished_at.isoformat() if lesson.finished_at else None,
        },
        "summary": {
            "total": round(sum(card["total"] for card in cards) / len(cards), 2) if cards else None,
            "cards_total": len(cards),
            "cards_closed": sum(row.state == "CLOSED" for row in rows),
            "cards_expired": sum(
                row.primary_status_at is None
                or bool(
                    row.delivered_at
                    and (row.primary_status_at - row.delivered_at).total_seconds()
                    > lesson.settings["primary_status_deadline_sec"]
                )
                for row in rows
            ),
            "axes": axes,
        },
        "cards": cards,
    }


@router.get("/state")
async def state(db: DB, user: Student) -> dict[str, Any]:
    now = datetime.now(UTC)
    row = (
        await db.execute(
            select(Lesson, LessonParticipant)
            .join(LessonParticipant)
            .where(LessonParticipant.student_id == user.id, Lesson.status == "RUNNING")
            .order_by(Lesson.started_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return {
            "server_time": now.isoformat(),
            "lesson": None,
            "workstation": None,
            "cards": [],
            "stats": {"closed": 0, "expired": 0, "avg_primary_delay_ms": None},
        }
    lesson, participant = row
    workstation = (
        await db.get(Workstation, participant.workstation_id)
        if participant.workstation_id
        else None
    )
    assignments = list(
        await db.scalars(
            select(Assignment)
            .where(
                Assignment.lesson_id == lesson.id,
                Assignment.student_id == user.id,
                Assignment.state != "QUEUED",
            )
            .order_by(Assignment.delivered_at)
        )
    )
    summaries = await card_summaries(db, assignments, lesson, now)
    delays = [
        (assignment.primary_status_at - assignment.delivered_at).total_seconds() * 1000
        for assignment in assignments
        if assignment.primary_status_at and assignment.delivered_at
    ]
    return {
        "server_time": now.isoformat(),
        "lesson": lesson_payload(lesson),
        "workstation": {"number": workstation.number} if workstation else None,
        "cards": summaries,
        "stats": {
            "closed": sum(row.state == "CLOSED" for row in assignments),
            "expired": sum(row["is_overdue"] for row in summaries),
            "avg_primary_delay_ms": round(sum(delays) / len(delays)) if delays else None,
        },
    }


@router.get("/assignments/{assignment_id}")
async def assignment_detail(assignment_id: UUID, db: DB, user: Student) -> dict[str, Any]:
    assignment, lesson = await own_assignment(db, assignment_id, user.id, lock=True)
    now = datetime.now(UTC)
    opened = (
        assignment.opened_at is None
        and assignment.state != "CLOSED"
        and lesson.status == "RUNNING"
        and not (
            assignment.task_mode == "CARD_ENTRY"
            and lesson.settings.get("incoming_channel") == "VOICE"
        )
    )
    if opened:
        assignment.opened_at = now
        if assignment.task_mode == "CARD_ENTRY":
            assignment.primary_status_at = now
            assignment.state = "PRIMARY_SET"
        if assignment.state == "DELIVERED":
            assignment.state = "OPENED"
        assert assignment.delivered_at is not None
        db.add(
            StatusEvent(
                assignment_id=assignment.id,
                status="RECEIVED",
                is_automatic=True,
                elapsed_ms=max(0, round((now - assignment.delivered_at).total_seconds() * 1000)),
                created_at=now,
            )
        )
        db.add(InteractionEvent(assignment_id=assignment.id, kind="CARD_OPENED", created_at=now))
        if assignment.task_mode == "CARD_ENTRY":
            db.add(
                InteractionEvent(
                    assignment_id=assignment.id, kind="INCOMING_ACCEPTED", created_at=now
                )
            )
            db.add(
                AuditLog(
                    user_id=user.id,
                    action="INCOMING_ACCEPTED",
                    entity_type="assignment",
                    entity_id=assignment.id,
                    created_at=now,
                )
            )
        db.add(
            AuditLog(
                user_id=user.id,
                action="CARD_OPENED",
                entity_type="assignment",
                entity_id=assignment.id,
                created_at=now,
            )
        )
        await db.flush()
    payload = await full_card(db, assignment, lesson, user, now)
    await db.commit()
    if opened:
        await hub.send(
            f"user:{lesson.teacher_id}",
            "STUDENT_ACTION",
            {
                "student_id": str(user.id),
                "assignment_id": str(assignment.id),
                "kind": "CARD_OPENED",
                "status": "RECEIVED",
            },
            now,
        )
    return payload
