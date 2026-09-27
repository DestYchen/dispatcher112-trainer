import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Service, Street, User
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_user_create_edit_duplicate_service_and_redaction(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "admin", "admin")
    service = await db.scalar(select(Service.id).limit(1))
    body: dict[str, Any] = {
        "login": "New.User",
        "password": "new-password",
        "first_name": "Анна",
        "last_name": "Петрова",
        "role": "STUDENT",
        "service_id": str(service),
    }
    created = await client.post("/api/v1/admin/users", json=body)
    assert created.status_code == 201 and created.json()["login"] == "new.user"
    assert "password" not in created.text and "totp_secret" not in created.text
    row_id = created.json()["id"]
    assert (await client.post("/api/v1/admin/users", json=body)).status_code == 400
    assert (
        await client.post(
            "/api/v1/admin/users", json={**body, "login": "missing", "service_id": None}
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/admin/users", json={**body, "login": "missing", "service_id": str(uuid4())}
        )
    ).status_code == 400
    changed = await client.patch(
        "/api/v1/admin/users", json={"id": row_id, "last_name": "Сидорова"}
    )
    assert changed.status_code == 200 and changed.json()["last_name"] == "Сидорова"
    assert (
        await client.patch("/api/v1/admin/users", json={"id": row_id, "first_name": None})
    ).status_code == 400
    assert (
        await client.patch("/api/v1/admin/users", json={"id": str(uuid4()), "first_name": "Анна"})
    ).status_code == 404
    assert "password_hash" not in (await client.get("/api/v1/admin/users")).text
    actions = list(await db.scalars(select(AuditLog.action)))
    assert "USER_CREATED" in actions and "USER_UPDATED" in actions


async def test_last_admin_and_active_lesson_roles_are_protected(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, rows = await lesson_fixture(db, 1)
    await sign_in(client, "admin", "admin")
    admin_id = await db.scalar(select(User.id).where(User.login == "admin"))
    assert (await client.post(f"/api/v1/admin/users/{admin_id}/block")).status_code == 400
    assert (
        await client.patch("/api/v1/admin/users", json={"id": str(admin_id), "role": "TEACHER"})
    ).status_code == 400
    assert (
        await client.patch(
            "/api/v1/admin/users", json={"id": str(rows[0].student_id), "role": "TEACHER"}
        )
    ).status_code == 409


async def test_password_reset_revokes_old_session_and_block_unblock(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client)
    old = client.cookies.get("session")
    student_id = await db.scalar(select(User.id).where(User.login == "student1"))
    client.cookies.clear()
    await sign_in(client, "admin", "admin")
    admin_cookie = client.cookies.get("session")
    csrf = client.headers["X-CSRF-Token"]
    assert (
        await client.post(
            f"/api/v1/admin/users/{student_id}/reset-password",
            json={"password": "changed-password"},
        )
    ).status_code == 200
    client.cookies.clear()
    client.cookies.set("session", old or "")
    assert (await client.get("/api/v1/student/state")).status_code == 401
    client.cookies.clear()
    await sign_in(client, "student1", "changed-password")
    assert (await client.get("/api/v1/student/state")).status_code == 200
    student_cookie = client.cookies.get("session")
    client.cookies.clear()
    client.cookies.set("session", admin_cookie or "")
    client.headers["X-CSRF-Token"] = csrf
    assert (await client.post(f"/api/v1/admin/users/{student_id}/block")).status_code == 200
    assert (
        await client.patch("/api/v1/admin/users", json={"id": str(student_id), "is_active": True})
    ).status_code == 200
    client.cookies.clear()
    client.cookies.set("session", student_cookie or "")
    assert (await client.get("/api/v1/student/state")).status_code == 401
    payloads = str(list(await db.scalars(select(AuditLog.payload))))
    assert "changed-password" not in payloads


async def test_workstations_unique_and_audited(client: AsyncClient, db: AsyncSession) -> None:
    await sign_in(client, "admin", "admin")
    body = {"number": "АРМ-88", "room": "Класс 2"}
    assert (await client.post("/api/v1/admin/workstations", json=body)).status_code == 201
    assert (await client.post("/api/v1/admin/workstations", json=body)).status_code == 400
    assert any(
        row["number"] == "АРМ-88"
        for row in (await client.get("/api/v1/admin/workstations")).json()["items"]
    )
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "WORKSTATION_CREATED")
        )
        or 0
    ) >= 1


async def test_imports_are_atomic_idempotent_and_audited_by_actor(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "admin", "admin")
    before = await db.scalar(select(func.count()).select_from(Street))
    assert before is not None
    invalid = "name,district\nПравильная,Район\n,Район\n".encode()
    assert (
        await client.post("/api/v1/admin/streets/import", files={"file": ("streets.csv", invalid)})
    ).status_code == 400
    assert await db.scalar(select(func.count()).select_from(Street)) == before
    for _ in range(2):
        assert (
            await client.post(
                "/api/v1/admin/streets/import",
                files={"file": ("streets.csv", "name,district\nУчебная улица,Район\n".encode())},
            )
        ).status_code == 200
    assert await db.scalar(select(func.count()).select_from(Street)) == before + 1
    content = await asyncio.to_thread(Path("/data/classifier.xlsx").read_bytes)
    imported = await client.post(
        "/api/v1/admin/classifier/import", files={"file": ("classifier.xlsx", content)}
    )
    assert imported.status_code == 200 and imported.json()["incident_types"] >= 1200
    assert (
        await client.post(
            "/api/v1/admin/classifier/import", files={"file": ("bad.xlsx", b"broken")}
        )
    ).status_code == 400
    assert (
        await client.post("/api/v1/admin/streets/import", files={"file": ("empty.csv", b"")})
    ).status_code == 400
    actor = await db.scalar(select(User.id).where(User.login == "admin"))
    assert await db.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "CLASSIFIER_IMPORTED", AuditLog.user_id == actor
        )
    )


async def test_audit_filters_pagination_and_bad_dates(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "admin", "admin")
    actor = await db.scalar(select(User.id).where(User.login == "admin"))
    now = datetime.now(UTC)
    db.add_all(
        [
            AuditLog(user_id=actor, action="AUDIT_TEST", created_at=now + timedelta(seconds=i))
            for i in range(3)
        ]
    )
    await db.flush()
    params: dict[str, str | int] = {
        "user_id": str(actor),
        "from": now.isoformat(),
        "to": (now + timedelta(seconds=5)).isoformat(),
        "limit": 2,
    }
    first = (await client.get("/api/v1/admin/audit", params=params)).json()
    second = (
        await client.get("/api/v1/admin/audit", params={**params, "cursor": first["next_cursor"]})
    ).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1 and second["next_cursor"] is None
    assert len({row["id"] for row in first["items"] + second["items"]}) == 3
    assert (await client.get("/api/v1/admin/audit", params={"cursor": "bad"})).status_code == 400
    assert (
        await client.get("/api/v1/admin/audit", params={"from": "2026-01-01T00:00:00"})
    ).status_code == 400


async def test_health_probes_are_live_and_worker_not_invented(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(client, "admin", "admin")

    async def failed(url: str) -> dict[str, Any]:
        assert url.endswith("/v2/languages")
        return {"ok": False, "latency_ms": 1500}

    monkeypatch.setattr("app.api.admin_system.http_probe", failed)
    data = (await client.get("/api/v1/admin/health")).json()
    assert data["database"]["ok"] and data["redis"]["ok"]
    assert not data["worker"]["ok"] and data["worker"]["queue_depth"] == 0
    assert not data["languagetool"]["ok"]
    assert data["generation_backend"] == {"kind": "template", "ok": True}


@pytest.mark.parametrize(
    "route", ["users", "workstations", "services", "streets", "audit", "health", "classifier/stats"]
)
async def test_admin_reads_are_forbidden_to_student(client: AsyncClient, route: str) -> None:
    await sign_in(client)
    assert (await client.get("/api/v1/admin/" + route)).status_code == 403
