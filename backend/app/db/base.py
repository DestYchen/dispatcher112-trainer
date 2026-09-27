from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.domain.runtime_configuration import DatabaseParameters


class Base(DeclarativeBase):
    """SQLAlchemy metadata shared by canonical database tables."""


def database_engine(parameters: DatabaseParameters, role: str) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=parameters.pool_size(role),
        max_overflow=parameters.max_overflow,
        pool_timeout=parameters.pool_timeout_seconds,
        connect_args={
            "server_settings": {
                "statement_timeout": str(parameters.statement_timeout_ms),
                "lock_timeout": str(parameters.lock_timeout_ms),
            }
        },
    )


engine = database_engine(DatabaseParameters(), "backend")
session_factory = async_sessionmaker(engine, expire_on_commit=False)
