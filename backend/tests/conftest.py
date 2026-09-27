import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.transport_security import redis_tls

TEST_URL = (
    make_url(settings.database_url)
    .set(database="dispatcher_test")
    .render_as_string(hide_password=False)
)


@pytest.fixture(scope="session")
def prepared_database() -> Iterator[None]:
    async def prepare() -> None:
        admin = create_async_engine(settings.database_url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = 'dispatcher_test'")
            )
            if not exists:
                await connection.execute(text("CREATE DATABASE dispatcher_test"))
        await admin.dispose()

    asyncio.run(prepare())
    env = {**os.environ, "DATABASE_URL": TEST_URL}
    subprocess.run(["alembic", "upgrade", "head"], check=True, env=env)

    async def seed() -> None:
        from app.operations.database import grant_backup_access, grant_runtime_access
        from app.seeds.create_demo_users import create_demo_users
        from app.seeds.directory import seed_directory
        from app.seeds.import_classifier import import_classifier
        from app.seeds.import_streets import import_streets

        engine = create_async_engine(TEST_URL, hide_parameters=True)
        async with engine.begin() as connection:
            await grant_runtime_access(connection, os.environ["APP_DB_PASSWORD"])
            await grant_backup_access(connection, os.environ["BACKUP_DB_PASSWORD"])
        async with AsyncSession(engine) as session, session.begin():
            await import_classifier(session, Path("/data/classifier.xlsx"))
            await import_streets(session, Path("/data/streets.csv"))
            await create_demo_users(session)
            await seed_directory(session)
        await engine.dispose()

    asyncio.run(seed())
    yield


@pytest.fixture
async def db(prepared_database: None) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_URL, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.api.deps import get_db, get_redis
    from app.main import app

    cache: Redis = Redis.from_url(
        settings.redis_url.rsplit("/", 1)[0] + "/15", decode_responses=True, **redis_tls()
    )
    await cache.flushdb()

    async def test_db() -> AsyncIterator[AsyncSession]:
        yield db

    async def test_cache() -> AsyncIterator[Redis]:
        yield cache

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_redis] = test_cache
    scheme = "https" if settings.cookie_secure else "http"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"{scheme}://test"
    ) as connection:
        yield connection
    app.dependency_overrides.clear()
    await cache.aclose()
