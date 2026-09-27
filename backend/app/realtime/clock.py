import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import Assignment, AuditLog, InteractionEvent, Lesson, StatusEvent
from app.domain.cards import card_summaries
from app.domain.maintenance import MAINTENANCE_LOCK, maintenance_state
from app.realtime.hub import hub
from app.realtime.teacher_updates import teacher_updates

logger = logging.getLogger(__name__)
Push = tuple[str, str, dict[str, Any]]


async def tick(db: AsyncSession, now: datetime) -> list[Push]:
    await db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": MAINTENANCE_LOCK})
    if (await maintenance_state(db))["enabled"]:
        return []
    messages: list[Push] = []
    lessons = list(
        await db.scalars(
            select(Lesson)
            .where(Lesson.status == "RUNNING")
            .order_by(Lesson.id)
            .with_for_update(skip_locked=True)
        )
    )
    for lesson in lessons:
        assignments = list(
            await db.scalars(
                select(Assignment)
                .where(Assignment.lesson_id == lesson.id)
                .order_by(Assignment.created_at, Assignment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for assignment in assignments:
            if assignment.state in {"QUEUED", "CLOSED"}:
                continue
            if (
                assignment.primary_status_at is None
                and assignment.delivered_at
                and (now - assignment.delivered_at).total_seconds()
                > lesson.settings["primary_status_deadline_sec"]
                and assignment.state != "EXPIRED"
            ):
                assignment.state = "EXPIRED"
                db.add(
                    InteractionEvent(
                        assignment_id=assignment.id, kind="PRIMARY_EXPIRED", created_at=now
                    )
                )
                db.add(
                    AuditLog(
                        action="CARD_EXPIRED",
                        entity_type="assignment",
                        entity_id=assignment.id,
                        created_at=now,
                    )
                )
                messages.append(
                    (
                        f"user:{assignment.student_id}",
                        "CARD_EXPIRED",
                        {"assignment_id": str(assignment.id)},
                    )
                )
            if (
                assignment.opened_at
                and (now - assignment.opened_at).total_seconds()
                > lesson.settings["card_processing_deadline_sec"]
            ):
                recorded = await db.scalar(
                    select(InteractionEvent.id).where(
                        InteractionEvent.assignment_id == assignment.id,
                        InteractionEvent.kind == "PROCESSING_EXPIRED",
                    )
                )
                if recorded is None:
                    db.add(
                        InteractionEvent(
                            assignment_id=assignment.id, kind="PROCESSING_EXPIRED", created_at=now
                        )
                    )
                    db.add(
                        AuditLog(
                            action="PROCESSING_EXPIRED",
                            entity_type="assignment",
                            entity_id=assignment.id,
                            created_at=now,
                        )
                    )
        deliveries = []
        for student_id in sorted({row.student_id for row in assignments}):
            rows = [row for row in assignments if row.student_id == student_id]
            active = sum(row.state in {"DELIVERED", "OPENED", "PRIMARY_SET"} for row in rows)
            delivered = [row.delivered_at for row in rows if row.delivered_at]
            latest = max(delivered) if delivered else None
            queued = next((row for row in rows if row.state == "QUEUED"), None)
            if (
                queued
                and active < lesson.settings["max_concurrent_cards"]
                and (
                    latest is None
                    or (now - latest).total_seconds() >= lesson.settings["card_interval_sec"]
                )
            ):
                queued.state, queued.delivered_at = "DELIVERED", now
                db.add(
                    StatusEvent(
                        assignment_id=queued.id,
                        status="ADDED",
                        is_automatic=True,
                        elapsed_ms=0,
                        created_at=now,
                    )
                )
                db.add(
                    AuditLog(
                        action="CARD_DELIVERED",
                        entity_type="assignment",
                        entity_id=queued.id,
                        created_at=now,
                    )
                )
                deliveries.append(queued)
                messages.append(
                    (
                        f"user:{lesson.teacher_id}",
                        "STUDENT_ACTION",
                        {
                            "student_id": str(student_id),
                            "assignment_id": str(queued.id),
                            "kind": "CARD_DELIVERED",
                            "status": "ADDED",
                        },
                    )
                )
        if deliveries:
            await db.flush()
            summaries = await card_summaries(db, deliveries, lesson, now)
            messages.extend(
                (f"user:{row.student_id}", "CARD_DELIVERED", summary)
                for row, summary in zip(deliveries, summaries, strict=True)
            )
    await db.flush()
    return messages


async def claim_clock_tick(cache: Redis) -> bool:
    # A short expiring slot prevents each replica from recalculating the same live view.
    # A failed process cannot retain leadership; PostgreSQL still serializes mutations.
    return bool(await cache.set("dispatcher:clock-slot", "1", nx=True, px=950))


async def run_clock() -> None:
    while True:
        started = asyncio.get_running_loop().time()
        try:
            if hub.cache is None or await claim_clock_tick(hub.cache):
                now = datetime.now(UTC)
                async with session_factory() as db:
                    messages = []
                    leader = bool(await db.scalar(text("SELECT pg_try_advisory_xact_lock(112160)")))
                    if leader:
                        messages = await tick(db, now)
                    await db.commit()
                if leader:
                    await hub.send_many(messages, now)
                    # Read-only live views must not hold exclusive lesson locks.
                    async with session_factory() as db:
                        updates = await teacher_updates(db, datetime.now(UTC))
                    await hub.send_many(updates, datetime.now(UTC))
        except Exception:
            logger.exception("Ошибка тика планировщика; транзакция отменена")
        await asyncio.sleep(max(0.01, 1 - (asyncio.get_running_loop().time() - started)))
