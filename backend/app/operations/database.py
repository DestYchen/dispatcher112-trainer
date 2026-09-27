"""Installation-only grants; the runtime role never owns the database or schema."""

import asyncio
import os
import subprocess

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import settings

RUNTIME_ROLE = "dispatcher_app"
BACKUP_ROLE = "dispatcher_backup"


async def grant_runtime_access(connection: AsyncConnection, password: str) -> None:
    if len(password) < 32:
        raise ValueError("Application database password must contain at least 32 characters")
    exists = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": RUNTIME_ROLE}
    )
    if not exists:
        await connection.execute(text("CREATE ROLE dispatcher_app LOGIN"))
    try:
        command = await connection.scalar(
            text(
                "SELECT format('ALTER ROLE dispatcher_app WITH LOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', "
                "CAST(:password AS text))"
            ),
            {"password": password},
        )
        await connection.exec_driver_sql(str(command))
    except DBAPIError:
        # ALTER ROLE embeds its password: never propagate its statement into a traceback.
        raise ValueError("Application database account could not be configured") from None
    await connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC, dispatcher_app"))
    await connection.execute(text("GRANT USAGE ON SCHEMA public TO dispatcher_app"))
    await connection.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dispatcher_app"))
    await connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dispatcher_app"
        )
    )
    await connection.execute(text("REVOKE UPDATE, DELETE ON audit_log FROM dispatcher_app"))
    await connection.execute(
        text("REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM dispatcher_app")
    )
    await connection.execute(
        text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dispatcher_app")
    )
    # Grant only after each completed migration; new audit tables must be reviewed explicitly.


async def grant_backup_access(connection: AsyncConnection, password: str) -> None:
    if len(password) < 32:
        raise ValueError("Backup database password must contain at least 32 characters")
    exists = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": BACKUP_ROLE}
    )
    if not exists:
        await connection.execute(text("CREATE ROLE dispatcher_backup LOGIN"))
    owns_source = await connection.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_shdepend d "
            "WHERE d.refclassid='pg_authid'::regclass "
            "AND d.refobjid=(SELECT oid FROM pg_roles WHERE rolname='dispatcher_backup') "
            "AND d.deptype='o' AND ("
            "d.dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) OR "
            "(d.classid='pg_database'::regclass AND "
            "d.objid=(SELECT oid FROM pg_database WHERE datname=current_database()))))"
        )
    )
    if owns_source:
        raise ValueError("Backup role owns source database objects; transfer ownership first")
    memberships = await connection.scalars(
        text(
            "SELECT format('REVOKE %I FROM dispatcher_backup', parent.rolname) "
            "FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid "
            "WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname='dispatcher_backup')"
        )
    )
    for revoke in memberships:
        await connection.exec_driver_sql(revoke)
    try:
        command = await connection.scalar(
            text(
                "SELECT format('ALTER ROLE dispatcher_backup WITH LOGIN NOSUPERUSER CREATEDB "
                "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L', "
                "CAST(:password AS text))"
            ),
            {"password": password},
        )
        await connection.exec_driver_sql(str(command))
    except DBAPIError:
        raise ValueError("Backup database account could not be configured") from None
    for action in ("REVOKE ALL ON DATABASE %I FROM", "GRANT CONNECT ON DATABASE %I TO"):
        command = await connection.scalar(
            text("SELECT format(:command, current_database())"),
            {"command": action + " dispatcher_backup"},
        )
        await connection.exec_driver_sql(str(command))
    await connection.execute(text("REVOKE ALL ON SCHEMA public FROM dispatcher_backup"))
    await connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    await connection.execute(text("GRANT USAGE ON SCHEMA public TO dispatcher_backup"))
    await connection.execute(
        text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dispatcher_backup")
    )
    await connection.execute(
        text(
            "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            "ON ALL TABLES IN SCHEMA public FROM PUBLIC"
        )
    )
    await connection.execute(
        text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO dispatcher_backup")
    )
    await connection.execute(
        text("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, dispatcher_backup")
    )
    await connection.execute(
        text("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO dispatcher_backup")
    )


async def provision() -> None:
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await grant_runtime_access(connection, os.environ["APP_DB_PASSWORD"])
            await grant_backup_access(connection, os.environ["BACKUP_DB_PASSWORD"])
    finally:
        await engine.dispose()


def main() -> None:
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    asyncio.run(provision())
    print("Schema migrated; application and backup roles configured with bounded permissions.")


if __name__ == "__main__":
    main()
