import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.operations.database import grant_backup_access
from tests.conftest import TEST_URL


@pytest.fixture
async def backup_connection(prepared_database: None) -> AsyncIterator[AsyncConnection]:
    url = make_url(TEST_URL).set(
        username="dispatcher_backup", password=os.environ["BACKUP_DB_PASSWORD"]
    )
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


async def test_backup_role_is_readonly_in_source_and_can_create_verification_databases(
    backup_connection: AsyncConnection,
) -> None:
    row = (
        await backup_connection.execute(
            text(
                "SELECT rolname, rolcreatedb, rolsuper, rolcreaterole, rolinherit, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
        )
    ).one()
    assert row.rolname == "dispatcher_backup" and row.rolcreatedb
    assert row.rolinherit  # Only implicit pg_database_owner in its own verification DB.
    assert not any((row.rolsuper, row.rolcreaterole, row.rolreplication, row.rolbypassrls))
    assert await backup_connection.scalar(text("SELECT count(*) FROM incident_types")) == 1200
    assert not await backup_connection.scalar(
        text(
            "SELECT count(*) FROM pg_auth_members WHERE member="
            "(SELECT oid FROM pg_roles WHERE rolname=current_user)"
        )
    )
    assert not await backup_connection.scalar(
        text("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")
    )
    for operation in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        permitted = await backup_connection.scalar(
            text("SELECT has_table_privilege(current_user, 'audit_log', :operation)"),
            {"operation": operation},
        )
        assert permitted is (operation == "SELECT")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO system_settings (key,value) SELECT 'denied','{}' WHERE false",
        "UPDATE system_settings SET value='{}' WHERE false",
        "DELETE FROM system_settings WHERE false",
        "INSERT INTO audit_log (action,entity_type) SELECT 'DENIED','check' WHERE false",
        "UPDATE audit_log SET payload='{}' WHERE false",
        "DELETE FROM audit_log WHERE false",
        "TRUNCATE audit_log",
        "ALTER TABLE audit_log DISABLE TRIGGER audit_immutable",
        "CREATE TABLE public.backup_forbidden_table (id integer)",
        "CREATE SCHEMA backup_forbidden_schema",
        "CREATE ROLE backup_forbidden_role",
        "SET ROLE dispatcher_app",
        "SET ROLE pg_database_owner",
        "SELECT rolpassword FROM pg_authid",
        "SELECT pg_read_file('/etc/passwd')",
    ],
)
async def test_backup_cannot_change_source_or_escalate_permissions(
    backup_connection: AsyncConnection, statement: str
) -> None:
    async with backup_connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as error:
            await backup_connection.execute(text(statement))
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
        await savepoint.rollback()


async def test_backup_can_create_write_and_drop_only_its_verification_database(
    prepared_database: None,
) -> None:
    url = make_url(TEST_URL).set(
        username="dispatcher_backup", password=os.environ["BACKUP_DB_PASSWORD"]
    )
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    database = "dispatcher_verify_permission_" + uuid4().hex[:12]
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}" TEMPLATE template0'))
            try:
                restored = create_async_engine(url.set(database=database), hide_parameters=True)
                try:
                    async with restored.begin() as target:
                        await target.execute(text("CREATE EXTENSION pg_trgm"))
                        await target.execute(text("CREATE TABLE restored_data (value integer)"))
                        await target.execute(text("INSERT INTO restored_data VALUES (112)"))
                        assert await target.scalar(text("SELECT value FROM restored_data")) == 112
                finally:
                    await restored.dispose()
                owner = await connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(datdba) FROM pg_database "
                        "WHERE datname=current_database()"
                    )
                )
                assert owner != "dispatcher_backup"
                with pytest.raises(DBAPIError) as error:
                    await connection.execute(text("ALTER DATABASE dispatcher_test RESET ALL"))
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
            finally:
                await connection.execute(text(f'DROP DATABASE "{database}"'))
    finally:
        await engine.dispose()


async def test_backup_provisioning_removes_unexpected_grants_and_role_memberships(
    prepared_database: None,
) -> None:
    engine = create_async_engine(TEST_URL, hide_parameters=True)
    try:
        async with engine.connect() as admin:
            transaction = await admin.begin()
            try:
                await admin.execute(text("GRANT dispatcher_app TO dispatcher_backup"))
                await admin.execute(text("GRANT UPDATE ON audit_log TO dispatcher_backup"))
                await admin.execute(text("GRANT UPDATE ON audit_log TO PUBLIC"))
                await admin.execute(text("CREATE SEQUENCE backup_sequence_probe"))
                await admin.execute(
                    text("GRANT USAGE, UPDATE ON SEQUENCE backup_sequence_probe TO PUBLIC")
                )
                for _ in range(2):
                    await grant_backup_access(admin, os.environ["BACKUP_DB_PASSWORD"])
                assert not await admin.scalar(
                    text("SELECT has_table_privilege('dispatcher_backup', 'audit_log', 'UPDATE')")
                )
                assert not await admin.scalar(
                    text("SELECT pg_has_role('dispatcher_backup', 'dispatcher_app', 'MEMBER')")
                )
                for operation in ("SELECT", "USAGE", "UPDATE"):
                    allowed = await admin.scalar(
                        text(
                            "SELECT has_sequence_privilege('dispatcher_backup', "
                            "'backup_sequence_probe', :operation)"
                        ),
                        {"operation": operation},
                    )
                    assert allowed is (operation == "SELECT")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def test_backup_role_cannot_be_provisioned_as_source_object_owner(
    prepared_database: None,
) -> None:
    engine = create_async_engine(TEST_URL, hide_parameters=True)
    try:
        async with engine.connect() as admin:
            transaction = await admin.begin()
            try:
                await admin.execute(text("CREATE TABLE backup_owner_probe (id integer)"))
                await admin.execute(
                    text("ALTER TABLE backup_owner_probe OWNER TO dispatcher_backup")
                )
                with pytest.raises(ValueError, match="owns"):
                    await grant_backup_access(admin, os.environ["BACKUP_DB_PASSWORD"])
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def test_short_backup_password_is_rejected_before_ddl(
    backup_connection: AsyncConnection,
) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        await grant_backup_access(backup_connection, "short")
