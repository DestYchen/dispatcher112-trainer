"""Inspect real encrypted channels and reject plaintext; no certificate bypasses."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import httpx
from redis.asyncio import Redis
from redis.exceptions import AuthenticationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from websockets.asyncio.client import connect
from websockets.typing import Origin

from app.config import settings
from app.db.base import session_factory
from app.domain.sip import control_password
from app.transport_security import http_verify, redis_tls, verified_context


async def main() -> None:
    assert settings.tls_ca_file and settings.cookie_secure
    assert "POSTGRES_PASSWORD" not in os.environ and "APP_DB_PASSWORD" not in os.environ
    for path in ("/data/backups", "/backups", "/data/control/token"):
        assert not await asyncio.to_thread(Path(path).exists), path
    result: dict[str, Any] = {"passed": False, "https": [], "plaintext_rejected": []}
    urls = [
        "https://backend:8000/healthz",
        "https://frontend:5173/",
        "https://gateway:5173/healthz",
        settings.languagetool_url + "/v2/languages",
        settings.local_llm_url + "/api/tags",
    ]
    async with httpx.AsyncClient(verify=http_verify(), trust_env=False, timeout=10) as client:
        for url in urls:
            response = await client.get(url)
            response.raise_for_status()
            stream = response.extensions["network_stream"].get_extra_info("ssl_object")
            result["https"].append(
                {"url": url, "status": response.status_code, "tls": stream.version()}
            )
        ari = await client.get(
            settings.sip_ari_url + "/asterisk/info",
            auth=("dispatcher", control_password(settings.jwt_secret)),
        )
        ari.raise_for_status()
        result["ari_https"] = ari.status_code
        for host, port in [
            ("backend", 8000),
            ("frontend", 5173),
            ("gateway", 5173),
            ("languagetool", 8081),
            ("ollama", 11434),
            ("telephony", 8088),
        ]:
            try:
                plain = await client.get(f"http://{host}:{port}/healthz")
            except httpx.HTTPError:
                result["plaintext_rejected"].append(host)
            else:
                assert plain.status_code >= 400, (host, plain.status_code)
                result["plaintext_rejected"].append(host)
        pre = await client.get("https://backend:8000/api/v1/auth/me")
        assert pre.status_code == 401 and "; Secure" in pre.headers["set-cookie"]
        csrf = pre.json()["error"]["details"]["csrf_token"]
        signed = await client.post(
            "https://backend:8000/api/v1/auth/login",
            json={"login": "student1", "password": "student"},
            headers={"X-CSRF-Token": csrf},
        )
        signed.raise_for_status()
        assert "; Secure" in signed.headers["set-cookie"]
        token = client.cookies.get("session")
        async with connect(
            "wss://backend:8000/api/v1/ws?role=student",
            ssl=verified_context(settings.tls_ca_file),
            origin=Origin(settings.cors_origins[0]),
            additional_headers={"Cookie": f"session={token}"},
            proxy=None,
        ) as socket:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert event["type"] == "HEARTBEAT"
            result["application_wss"] = True
        await client.post(
            "https://backend:8000/api/v1/auth/logout",
            headers={"X-CSRF-Token": signed.json()["csrf_token"]},
        )
    async with session_factory() as db:
        row = (
            await db.execute(
                text("SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
            )
        ).one()
        assert row.ssl and row.version in {"TLSv1.2", "TLSv1.3"}
        result["postgres"] = {"ssl": row.ssl, "version": row.version, "cipher": row.cipher}
        role = (
            await db.execute(
                text(
                    "SELECT current_user AS name, rolsuper, rolcreatedb, rolcreaterole, "
                    "has_table_privilege(current_user, 'audit_log', 'UPDATE') AS audit_update, "
                    "has_table_privilege(current_user, 'audit_log', 'DELETE') AS audit_delete "
                    "FROM pg_roles WHERE rolname=current_user"
                )
            )
        ).one()
        assert role.name == "dispatcher_app" and not any(role[1:])
        result["database_runtime_role"] = dict(role._mapping)
    database_url = make_url(settings.database_url)
    try:
        connection = await asyncpg.connect(
            host=database_url.host,
            port=database_url.port or 5432,
            user=database_url.username,
            password=database_url.password,
            database=database_url.database,
            ssl=False,
        )
    except asyncpg.InvalidAuthorizationSpecificationError:
        result["plaintext_rejected"].append("postgres")
    else:
        await connection.close()
        raise AssertionError("PostgreSQL accepted plaintext")
    async with Redis.from_url(settings.redis_url, **redis_tls()) as cache:
        assert await cache.ping()
        result["redis_tls_authenticated"] = True
    async with Redis.from_url("rediss://redis:6379/0", **redis_tls()) as anonymous:
        try:
            await anonymous.ping()
        except AuthenticationError:
            result["redis_anonymous_rejected"] = True
        else:
            raise AssertionError("Redis accepted an anonymous TLS connection")
    writer = None
    try:
        reader, writer = await asyncio.open_connection("redis", 6379)
        writer.write(b"PING\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(128), timeout=3)
        assert b"PONG" not in data
    except (ConnectionError, TimeoutError):
        data = b""
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                data = b""
    result["plaintext_rejected"].append("redis")
    result["passed"] = True
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
