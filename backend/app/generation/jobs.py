import json
import logging
from typing import Any
from uuid import UUID

from arq import create_pool
from sqlalchemy import select, text

from app.db.base import session_factory
from app.db.models import AuditLog, GenerationJob, Lesson
from app.domain.maintenance import MAINTENANCE_LOCK, maintenance_state
from app.generation.builder import build_scenario
from app.transport_security import queue_settings

logger = logging.getLogger(__name__)


async def enqueue_generation(job_id: UUID) -> None:
    queue = await create_pool(queue_settings())
    try:
        await queue.enqueue_job("generate_scenarios", str(job_id), _job_id=str(job_id))
    finally:
        await queue.aclose()


async def dispatch_pending_jobs(ctx: dict[str, Any]) -> None:
    async with session_factory() as db:
        if (await maintenance_state(db))["enabled"]:
            return
        ids = list(
            await db.scalars(select(GenerationJob.id).where(GenerationJob.status == "QUEUED"))
        )
    for job_id in ids:
        await ctx["redis"].enqueue_job("generate_scenarios", str(job_id), _job_id=str(job_id))


async def generate_scenarios(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    identifier = UUID(job_id)
    try:
        while True:
            async with session_factory() as db:
                await db.execute(
                    text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": MAINTENANCE_LOCK}
                )
                if (await maintenance_state(db))["enabled"]:
                    return {"status": "DEFERRED"}
                job = await db.scalar(
                    select(GenerationJob).where(GenerationJob.id == identifier).with_for_update()
                )
                if job is None:
                    raise ValueError("Задача генерации не найдена.")
                if job.status in {"DONE", "FAILED"}:
                    return {
                        "status": job.status,
                        "generated": job.generated,
                        "rejected": job.rejected,
                    }
                lesson = await db.get(Lesson, job.lesson_id)
                if not lesson or lesson.status != "PLANNED":
                    raise ValueError("Генерация доступна только до начала занятия.")
                index = job.generated + job.rejected
                count = int(job.request["count"])
                if index >= count:
                    job.status, job.progress = "DONE", 1.0
                    await db.commit()
                    return {"status": "DONE", "generated": job.generated, "rejected": job.rejected}
                job.status = "RUNNING"
                scenario = await build_scenario(
                    db, job.lesson_id, job.user_id, job.request, identifier.int + index
                )
                db.add(scenario)
                await db.flush()
                if scenario.status == "REJECTED":
                    job.rejected += 1
                else:
                    job.generated += 1
                job.progress = (job.generated + job.rejected) / count
                db.add(
                    AuditLog(
                        user_id=job.user_id,
                        action="SCENARIO_GENERATED",
                        entity_type="scenario",
                        entity_id=scenario.id,
                        payload={"job_id": job_id, "status": scenario.status},
                    )
                )
                await db.commit()
                await ctx["redis"].publish(
                    "dispatcher:events",
                    json.dumps(
                        {
                            "room": f"user:{job.user_id}",
                            "type": "JOB_PROGRESS",
                            "payload": {
                                "job_id": job_id,
                                "progress": job.progress,
                                "generated": job.generated,
                                "rejected": job.rejected,
                            },
                        }
                    ),
                )
    except Exception as error:
        logger.exception("Генерация не завершена")
        async with session_factory() as db:
            job = await db.get(GenerationJob, identifier)
            if job:
                job.status = "FAILED"
                job.error = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Локальный генератор недоступен. "
                    "Проверьте его настройки и повторите подготовку."
                )
                db.add(
                    AuditLog(
                        user_id=job.user_id,
                        action="GENERATION_FAILED",
                        entity_type="generation_job",
                        entity_id=job.id,
                        payload={"error": job.error},
                    )
                )
                await db.commit()
        return {"status": "FAILED"}
