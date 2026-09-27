from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, Admin
from app.api.errors import APIError
from app.db.models import AuditLog, SystemSetting, User
from app.domain.access_policy import AccessPolicy, access_policy

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/policies")
async def policies(db: DB, user: Admin) -> dict[str, Any]:
    return {
        "access": (await access_policy(db)).model_dump(),
        "audit_retention": "indefinite",
        "audit_mutable": False,
    }


@router.patch("/policies")
async def update_policies(body: AccessPolicy, db: DB, user: Admin) -> dict[str, Any]:
    # User edits take this same lock, so enabling 2FA cannot race an administrator reset.
    admins = list(
        await db.scalars(
            select(User)
            .where(User.role == "ADMIN", User.is_active)
            .order_by(User.id)
            .with_for_update()
        )
    )
    if body.require_admin_totp and any(not admin.totp_secret for admin in admins):
        raise APIError(
            409,
            "ADMIN_TOTP_REQUIRED",
            "Сначала настройте двухфакторную защиту всех действующих администраторов.",
        )
    previous = (await access_policy(db)).model_dump()
    current = body.model_dump()
    if current != previous:
        row = await db.get(SystemSetting, "access_policy")
        if row is None:
            row = SystemSetting(key="access_policy")
            db.add(row)
        row.value, row.updated_by, row.updated_at = current, user.id, datetime.now(UTC)
        db.add(
            AuditLog(
                user_id=user.id,
                action="ACCESS_POLICY_UPDATED",
                entity_type="system_settings",
                payload={"previous": previous, "current": current},
            )
        )
        await db.commit()
    return await policies(db, user)
