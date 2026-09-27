import asyncio
from typing import Any

from arq import cron
from sqlalchemy import literal, select

from app import runtime_configuration
from app.db.base import session_factory
from app.generation.jobs import dispatch_pending_jobs, generate_scenarios
from app.logging import configure_logging
from app.transport_security import queue_settings


async def check_database(ctx: dict[str, Any]) -> dict[str, bool]:
    async with session_factory() as session:
        reachable = await session.scalar(select(literal(1))) == 1
    await ctx["redis"].set("dispatcher:database_ok", str(reachable), ex=120)
    return {"database": reachable}


async def startup(ctx: dict[str, Any]) -> None:
    ctx["configuration_task"] = await runtime_configuration.start("worker")


async def shutdown(ctx: dict[str, Any]) -> None:
    task = ctx.get("configuration_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class WorkerSettings:
    on_startup = startup
    on_shutdown = shutdown
    functions = [check_database, generate_scenarios]
    cron_jobs = [
        cron(check_database, minute=None, second=0, run_at_startup=True),
        cron(dispatch_pending_jobs, second=set(range(0, 60, 5)), run_at_startup=True),
    ]
    job_timeout = 1800
    redis_settings = queue_settings()
    health_check_interval = 5


configure_logging()
