from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Assignment, InteractionEvent, Lesson, LessonParticipant, User, Workstation
from app.realtime.hub import hub
from app.scoring.effective import effective_score


def is_overdue(row: Assignment, lesson: Lesson, now: datetime) -> bool:
    end = row.closed_at or now
    return bool(
        (
            row.delivered_at
            and ((row.primary_status_at or end) - row.delivered_at).total_seconds()
            > lesson.settings["primary_status_deadline_sec"]
        )
        or (
            row.opened_at
            and (end - row.opened_at).total_seconds()
            > lesson.settings["card_processing_deadline_sec"]
        )
    )


async def live_snapshot(db: AsyncSession, lesson: Lesson, now: datetime) -> dict[str, Any]:
    members = (
        await db.execute(
            select(LessonParticipant, User, Workstation)
            .join(User, User.id == LessonParticipant.student_id)
            .outerjoin(Workstation, Workstation.id == LessonParticipant.workstation_id)
            .where(LessonParticipant.lesson_id == lesson.id)
            .order_by(Workstation.number, User.last_name, User.first_name)
        )
    ).all()
    assignments = list(
        await db.scalars(
            select(Assignment).where(
                Assignment.lesson_id == lesson.id, Assignment.state != "QUEUED"
            )
        )
    )
    events = (
        await db.execute(
            select(InteractionEvent, Assignment.student_id)
            .join(Assignment, Assignment.id == InteractionEvent.assignment_id)
            .where(
                Assignment.lesson_id == lesson.id,
                InteractionEvent.kind.not_in(["PRIMARY_EXPIRED", "PROCESSING_EXPIRED"]),
            )
            .distinct(Assignment.student_id)
            .order_by(
                Assignment.student_id,
                InteractionEvent.created_at.desc(),
                InteractionEvent.id.desc(),
            )
        )
    ).all()
    latest = {student_id: event for event, student_id in events}
    by_student: dict[Any, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_student[assignment.student_id].append(assignment)
    students = []
    online = await hub.online_many([user.id for _member, user, _station in members])
    for _member, user, station in members:
        rows = by_student[user.id]
        active = [row for row in rows if row.state != "CLOSED"]
        event = latest.get(user.id)
        # Further deliveries must not reset inactivity when an older card is waiting.
        since = (
            event.created_at
            if event
            else min((row.delivered_at for row in active if row.delivered_at), default=now)
        )
        idle = bool(active and (now - since).total_seconds() > 90)
        overdue = any(is_overdue(row, lesson, now) for row in active)
        scores = [
            effective_score(row.score, row.teacher_override)["total"] for row in rows if row.score
        ]
        initials = " ".join(f"{part[0]}." for part in (user.first_name, user.middle_name) if part)
        students.append(
            {
                "student_id": str(user.id),
                "short_name": f"{user.last_name} {initials}",
                "workstation": station.number if station else None,
                "online": online[user.id],
                "active_cards": len(active),
                "closed": sum(row.state == "CLOSED" for row in rows),
                "expired": sum(is_overdue(row, lesson, now) for row in rows),
                "current_score": round(sum(scores) / len(scores), 2) if scores else None,
                "last_action": {
                    "kind": event.kind,
                    "status": (event.payload or {}).get("status"),
                    "at": event.created_at.isoformat(),
                }
                if event
                else None,
                "alert": ("IDLE" if idle else "OVERDUE" if overdue else None)
                if lesson.status == "RUNNING"
                else None,
            }
        )
    delays = [
        (row.primary_status_at - row.delivered_at).total_seconds() * 1000
        for row in assignments
        if row.primary_status_at and row.delivered_at
    ]
    violations: Counter[str] = Counter()
    messages = {}
    for row in assignments:
        for violation in (row.score or {}).get("violations", []):
            violations[violation["code"]] += 1
            messages[violation["code"]] = violation["message"]
    aggregate = {
        "cards_delivered": len(assignments),
        "cards_closed": sum(row.state == "CLOSED" for row in assignments),
        "cards_expired": sum(is_overdue(row, lesson, now) for row in assignments),
        "avg_primary_delay_ms": round(sum(delays) / len(delays)) if delays else None,
        "top_violations": [
            {"code": code, "count": count, "message": messages[code]}
            for code, count in sorted(violations.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
    }
    return {
        "server_time": now.isoformat(),
        "lesson_status": lesson.status,
        "elapsed_sec": max(
            0, int(((lesson.finished_at or now) - lesson.started_at).total_seconds())
        )
        if lesson.started_at
        else 0,
        "students": students,
        "aggregate": aggregate,
    }
