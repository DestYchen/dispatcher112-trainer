"""Measure individually committed audit writes in the isolated acceptance database."""

import asyncio
import json
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.db.models import AuditLog


async def main() -> None:
    target = make_url(settings.database_url).set(database="dispatcher_test")
    assert target.database == "dispatcher_test"
    engine = create_async_engine(target)
    run = uuid4()
    count = 500
    try:
        async with AsyncSession(engine) as db:
            await db.scalar(select(func.count()).select_from(AuditLog))
            await db.rollback()
            started = perf_counter()
            for _ in range(count):
                db.add(AuditLog(action="WRITE_ACCEPTANCE", entity_type="benchmark", entity_id=run))
                await db.commit()
            elapsed = perf_counter() - started
        async with AsyncSession(engine) as verifier:
            saved = await verifier.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == run)
            )
        assert saved == count
        result = {
            "committed_operations": count,
            "elapsed_seconds": round(elapsed, 3),
            "operations_per_second": round(count / elapsed, 2),
            "separate_transactions": True,
        }
        print(json.dumps(result))
        assert count / elapsed >= 100
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
