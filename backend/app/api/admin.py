from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import DB, Admin
from app.db.models import IncidentGroup, IncidentType, Service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/classifier/stats")
async def classifier_stats(db: DB, user: Admin) -> dict[str, int]:
    return {
        "groups": int(await db.scalar(select(func.count()).select_from(IncidentGroup)) or 0),
        "types": int(await db.scalar(select(func.count()).select_from(IncidentType)) or 0),
        "services": int(await db.scalar(select(func.count()).select_from(Service)) or 0),
    }
