"""Bounded operational parameters; source addresses and secrets are installation-owned."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KEY = "runtime_configuration"
ROLES = ("backend", "worker", "sip_worker")


class DatabaseParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_pool_size: int = Field(default=20, ge=5, le=50)
    worker_pool_size: int = Field(default=5, ge=1, le=20)
    sip_pool_size: int = Field(default=5, ge=1, le=10)
    max_overflow: int = Field(default=5, ge=0, le=10)
    pool_timeout_seconds: int = Field(default=10, ge=1, le=60)
    statement_timeout_ms: int = Field(default=30000, ge=500, le=30000)
    lock_timeout_ms: int = Field(default=3000, ge=100, le=10000)

    @model_validator(mode="after")
    def lock_budget(self) -> Self:
        if self.lock_timeout_ms > self.statement_timeout_ms:
            raise ValueError("Lock timeout cannot exceed the statement timeout")
        return self

    def pool_size(self, role: str) -> int:
        return {
            "backend": self.api_pool_size,
            "worker": self.worker_pool_size,
            "sip_worker": self.sip_pool_size,
        }[role]

    def connection_limit(self) -> int:
        return (
            self.api_pool_size + self.worker_pool_size + self.sip_pool_size + 3 * self.max_overflow
        )


class SipParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    inbound_ring_seconds: int = Field(default=40, ge=5, le=120)
    unanswered_seconds: int = Field(default=60, ge=5, le=120)
    max_call_seconds: int = Field(default=3600, ge=60, le=3600)

    @model_validator(mode="after")
    def call_budgets(self) -> Self:
        if not self.inbound_ring_seconds <= self.unanswered_seconds <= self.max_call_seconds:
            raise ValueError("Ringing and unanswered limits must fit within the call lifetime")
        return self


class LoggingParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    level: Literal["INFO", "WARNING", "ERROR"] = "INFO"
    http_access: bool = True


class RuntimeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: DatabaseParameters = Field(default_factory=DatabaseParameters)
    sip: SipParameters = Field(default_factory=SipParameters)
    logging: LoggingParameters = Field(default_factory=LoggingParameters)


class ConfigurationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(default=0, ge=0)
    configuration: RuntimeConfiguration = Field(default_factory=RuntimeConfiguration)
    request_id: UUID | None = None
    actor_id: UUID | None = None
    reason: str = ""
    changed_at: datetime | None = None


async def read_snapshot(db: AsyncSession) -> ConfigurationSnapshot:
    value = await db.scalar(text("SELECT value FROM system_settings WHERE key=:key"), {"key": KEY})
    return ConfigurationSnapshot.model_validate(value) if value else ConfigurationSnapshot()
