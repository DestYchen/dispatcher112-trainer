from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import APIError
from app.db.models import SystemSetting


class AccessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_minutes: int = Field(default=480, ge=15, le=480)
    login_attempts: int = Field(default=5, ge=3, le=5)
    lockout_minutes: int = Field(default=15, ge=15, le=60)
    min_password_length: int = Field(default=8, ge=8, le=128)
    require_admin_totp: bool = False


async def access_policy(db: AsyncSession) -> AccessPolicy:
    row = await db.get(SystemSetting, "access_policy", populate_existing=True)
    return AccessPolicy.model_validate(row.value) if row else AccessPolicy()


async def validate_password_policy(db: AsyncSession, password: str) -> None:
    policy = await access_policy(db)
    if len(password) < policy.min_password_length:
        raise APIError(
            400,
            "VALIDATION_ERROR",
            f"Пароль должен содержать не менее {policy.min_password_length} символов.",
        )
