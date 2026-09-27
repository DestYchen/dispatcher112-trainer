from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import APIError
from app.db.models import SystemSetting

MAINTENANCE_LOCK = 112150


async def maintenance_state(db: AsyncSession) -> dict[str, object]:
    row = await db.get(SystemSetting, "maintenance", populate_existing=True)
    return dict(row.value) if row else {"enabled": False, "reason": ""}


async def protect_mutation(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": MAINTENANCE_LOCK})
    if (await maintenance_state(db))["enabled"]:
        raise APIError(
            503, "MAINTENANCE", "Идёт техническое обслуживание. Изменения временно недоступны."
        )
