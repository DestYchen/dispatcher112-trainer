import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.operations.database import grant_runtime_access
from tests.conftest import TEST_URL


@pytest.fixture
async def runtime(prepared_database: None) -> AsyncIterator[AsyncConnection]:
    url = make_url(TEST_URL).set(username="dispatcher_app", password=os.environ["APP_DB_PASSWORD"])
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                yield connection
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def test_runtime_uses_distinct_unprivileged_identity(runtime: AsyncConnection) -> None:
    row = (
        await runtime.execute(
            text(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).one()
    assert row.rolname == "dispatcher_app"
    assert not any(row[1:])
    assert not await runtime.scalar(
        text(
            "SELECT pg_has_role(current_user, datdba, 'MEMBER') "
            "FROM pg_database WHERE datname = current_database()"
        )
    )


async def test_runtime_can_read_and_write_application_data(runtime: AsyncConnection) -> None:
    assert await runtime.scalar(text("SELECT count(*) FROM incident_types"))
    await runtime.execute(text("DELETE FROM system_settings WHERE key='permission-test'"))
    await runtime.execute(
        text("INSERT INTO system_settings (key, value) VALUES ('permission-test', '{}')")
    )
    await runtime.execute(
        text(
            "UPDATE system_settings SET value = CAST(:value AS jsonb) WHERE key='permission-test'"
        ),
        {"value": '{"checked":true}'},
    )
    assert (
        await runtime.scalar(
            text("SELECT value->>'checked' FROM system_settings WHERE key='permission-test'")
        )
        == "true"
    )
    await runtime.execute(text("DELETE FROM system_settings WHERE key='permission-test'"))


@pytest.mark.parametrize(
    "command",
    [
        "UPDATE audit_log SET payload='{}' WHERE false",
        "DELETE FROM audit_log WHERE false",
        "ALTER TABLE audit_log DISABLE TRIGGER audit_immutable",
        "CREATE TABLE public.forbidden_permission_test (id integer)",
        "CREATE SCHEMA forbidden_permission_test",
        "UPDATE alembic_version SET version_num='forbidden' WHERE false",
        "SELECT rolpassword FROM pg_authid",
    ],
)
async def test_runtime_cannot_mutate_audit_or_schema(
    runtime: AsyncConnection, command: str
) -> None:
    async with runtime.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as error:
            await runtime.execute(text(command))
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
        await savepoint.rollback()


async def test_audit_has_only_read_and_insert_privileges(runtime: AsyncConnection) -> None:
    for operation in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        allowed = await runtime.scalar(
            text("SELECT has_table_privilege(current_user, 'audit_log', :operation)"),
            {"operation": operation},
        )
        assert allowed is (operation in {"SELECT", "INSERT"})


async def test_invalid_runtime_password_is_rejected_before_any_ddl(
    runtime: AsyncConnection,
) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        await grant_runtime_access(runtime, "short")
