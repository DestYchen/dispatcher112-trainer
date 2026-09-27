from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lesson
from app.domain.teacher_live import live_snapshot
from app.realtime.hub import hub

previous_alerts: dict[tuple[str, str], str | None] = {}


async def teacher_updates(db: AsyncSession, now: datetime) -> list[tuple[str, str, dict[str, Any]]]:
    messages = []
    active_keys = set()
    lessons = list(await db.scalars(select(Lesson).where(Lesson.status == "RUNNING")))
    for lesson in lessons:
        if not await hub.is_online(lesson.teacher_id):
            continue
        snapshot = await live_snapshot(db, lesson, now)
        room = f"user:{lesson.teacher_id}"
        messages.append((room, "AGGREGATE_UPDATED", snapshot["aggregate"]))
        for row in snapshot["students"]:
            key = (str(lesson.id), row["student_id"])
            active_keys.add(key)
            if row["alert"] != previous_alerts.get(key):
                messages.append(
                    (
                        room,
                        "STUDENT_ALERT",
                        {
                            "student_id": row["student_id"],
                            "alert": row["alert"],
                        },
                    )
                )
            previous_alerts[key] = row["alert"]
    for key in set(previous_alerts) - active_keys:
        del previous_alerts[key]
    return messages
