from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assignment,
    IncidentType,
    Lesson,
    LessonParticipant,
    Scenario,
    User,
    Workstation,
)
from app.scoring.effective import effective_score

REPORT_COLUMNS = ["Фамилия", "АРМ", "Время", "Ошибки", "Орфогр.", "Уровень", "Балл"]


def training_level(total: float | None) -> str | None:
    if total is None:
        return None
    return "уверенный" if total >= 85 else "базовый" if total >= 60 else "начальный"


async def lesson_report(db: AsyncSession, lesson: Lesson) -> dict[str, Any]:
    members = (
        await db.execute(
            select(User, Workstation)
            .select_from(LessonParticipant)
            .join(User, User.id == LessonParticipant.student_id)
            .outerjoin(Workstation, Workstation.id == LessonParticipant.workstation_id)
            .where(LessonParticipant.lesson_id == lesson.id)
            .order_by(User.last_name, User.first_name, User.id)
        )
    ).all()
    assignments = (
        await db.execute(
            select(Assignment, IncidentType)
            .join(Scenario, Scenario.id == Assignment.scenario_id)
            .join(IncidentType, IncidentType.id == Scenario.incident_type_id)
            .where(Assignment.lesson_id == lesson.id)
            .order_by(Assignment.card_number, Assignment.id)
        )
    ).all()
    now = lesson.finished_at or datetime.now(UTC)
    deadline = lesson.settings["primary_status_deadline_sec"]
    bins: list[dict[str, Any]] = [
        {"label": label, "count": 0}
        for label in ("До 15 с", "15–30 с", "30–60 с", "Больше 60 с", "Без первичного статуса")
    ]
    heat: dict[str, Counter[str]] = defaultdict(Counter)
    messages: dict[str, str] = {}
    by_student: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row, incident in assignments:
        if row.delivered_at is None:
            continue
        delay = ((row.primary_status_at or row.closed_at or now) - row.delivered_at).total_seconds()
        bin_index = (
            4
            if row.primary_status_at is None
            else (0 if delay <= 15 else 1 if delay <= 30 else 2 if delay <= 60 else 3)
        )
        bins[bin_index]["count"] += 1
        violations = (row.score or {}).get("violations", [])
        for violation in violations:
            heat[incident.name][violation["code"]] += 1
            messages[violation["code"]] = violation["message"]
        score = effective_score(row.score, row.teacher_override) if row.score else None
        by_student[str(row.student_id)].append(
            {
                "assignment_id": str(row.id),
                "card_number": row.card_number,
                "incident_type_name": incident.name,
                "state": row.state,
                "task_mode": row.task_mode,
                "processing_sec": round(((row.closed_at or now) - row.opened_at).total_seconds(), 3)
                if row.opened_at
                else None,
                "processing_deadline_sec": lesson.settings["card_processing_deadline_sec"],
                "primary_delay_sec": round(delay, 3),
                "primary_missing": row.primary_status_at is None,
                "score": row.score,
                "teacher_override": row.teacher_override,
                "total": score["total"] if score else None,
                "violations": violations,
            }
        )
    students = []
    for user, station in members:
        cards = by_student[str(user.id)]
        totals = [card["total"] for card in cards if card["total"] is not None]
        total = round(sum(totals) / len(totals), 2) if totals else None
        violations = [v for card in cards for v in card["violations"]]
        spelling = sum(v["code"] == "SPELLING" for v in violations)
        initials = " ".join(f"{name[0]}." for name in (user.first_name, user.middle_name) if name)
        students.append(
            {
                "student_id": str(user.id),
                "short_name": f"{user.last_name} {initials}",
                "workstation": station.number if station else None,
                "time_deviation_pct": round(
                    (sum(c["primary_delay_sec"] for c in cards) / len(cards) / deadline - 1) * 100,
                    1,
                )
                if cards
                else None,
                "errors": len(violations) - spelling,
                "spelling_errors": spelling,
                "level": training_level(total),
                "total": total,
                "manually_corrected": any(c["teacher_override"] is not None for c in cards),
                "cards": cards,
            }
        )
    return {
        "lesson": {
            "id": str(lesson.id),
            "title": lesson.title,
            "status": lesson.status,
            "started_at": lesson.started_at.isoformat() if lesson.started_at else None,
            "finished_at": lesson.finished_at.isoformat() if lesson.finished_at else None,
        },
        "columns": REPORT_COLUMNS,
        "students": students,
        "reaction_distribution": bins,
        "heatmap": {
            "violations": [{"code": code, "message": messages[code]} for code in sorted(messages)],
            "rows": [
                {"incident_type_name": name, "counts": dict(heat[name])} for name in sorted(heat)
            ],
        },
        "cards_total": sum(len(cards) for cards in by_student.values()),
    }
