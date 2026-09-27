import json
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.api.errors import APIError
from app.db.models import AuditLog, GenerationJob, SipCall
from app.main import app
from app.operations import diagnostics
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


@pytest.fixture(autouse=True)
def local_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "catalog",
        AsyncMock(return_value={"items": [{"valid_manifest": True, "verification": None}]}),
    )


@pytest.mark.parametrize("login,password", [("teacher", "teacher"), ("student1", "student")])
async def test_report_and_export_require_administrator(
    client: AsyncClient, login: str, password: str
) -> None:
    await sign_in(client, login, password)
    for export in ("false", "true"):
        assert (
            await client.get("/api/v1/admin/diagnostics/report", params={"export": export})
        ).status_code == 403


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01T00:00:00", "2026-01-02T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        ("2024-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ],
)
async def test_report_rejects_invalid_periods(client: AsyncClient, start: str, end: str) -> None:
    await sign_in(client, "admin", "admin")
    response = await client.get(
        "/api/v1/admin/diagnostics/report", params={"from": start, "to": end}
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_default_period_and_timezone_conversion() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    assert diagnostics.report_period(None, None, now) == (now - timedelta(days=1), now)
    start, end = diagnostics.report_period(
        datetime.fromisoformat("2026-01-01T03:00:00+03:00"),
        datetime.fromisoformat("2026-01-02T03:00:00+03:00"),
        now,
    )
    assert start == datetime(2026, 1, 1, tzinfo=UTC) and end - start == timedelta(days=1)
    for duration in (timedelta(seconds=1), timedelta(days=366)):
        assert diagnostics.report_period(now - duration, now, now) == (now - duration, now)
    with pytest.raises(APIError):
        diagnostics.report_period(now - timedelta(milliseconds=500), now, now)


async def test_period_counts_failures_pagination_export_and_privacy(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, assignments = await lesson_fixture(db, 2)
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    lesson.started_at = start
    assignments[0].delivered_at, assignments[0].closed_at = start, end
    assignments[1].delivered_at = end
    assignments[1].closed_at = start + timedelta(seconds=30)
    db.add_all(
        [
            AuditLog(
                action="SYSTEM_HTTP_ERROR",
                created_at=at,
                payload={
                    "request_id": f"{index:032x}",
                    "method": "POST",
                    "status": 503,
                    "code": "PRIVATE-UNSAFE-CODE",
                    "route": "/private/student/address",
                    "comment": "private-teaching-text",
                    "password": "private-password",
                },
            )
            for index, at in enumerate((start - timedelta(seconds=1), start, start, end))
        ]
        + [
            AuditLog(
                action="TECHNICAL_OPERATION_FINISHED",
                created_at=start,
                payload={"status": "FAILED", "error": "private-password"},
            ),
            AuditLog(
                action="STATUS_CHANGED",
                created_at=start,
                user_id=assignments[0].student_id,
                payload={"comment": "private-teaching-text"},
            ),
            GenerationJob(
                lesson_id=lesson.id,
                user_id=lesson.teacher_id,
                status="FAILED",
                created_at=start,
                updated_at=start,
                request={"private": "private-teaching-text"},
                error="private-password",
            ),
            SipCall(
                user_id=assignments[0].student_id,
                assignment_id=assignments[0].id,
                direction="INBOUND",
                state="FAILED",
                media_name="private-recording",
                created_at=start,
                ended_at=start,
                failure_reason="private-teaching-text",
            ),
        ]
    )
    await db.flush()
    await sign_in(client, "admin", "admin")
    params = {"from": start.isoformat(), "to": end.isoformat(), "limit": "2"}
    rows: list[dict[str, Any]] = []
    while True:
        response = await client.get("/api/v1/admin/diagnostics/report", params=params)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["usage"] == {
            "lessons_started": 1,
            "assignments_delivered": 1,
            "assignments_closed": 1,
            "sip_calls": 1,
            "generation_jobs": 1,
            "audit_events": 4,
            "active_users": 1,
        }
        assert value["failures"]["total"] == 5
        assert (
            "private-" not in response.text and str(assignments[0].student_id) not in response.text
        )
        rows.extend(value["failures"]["items"])
        cursor = value["failures"]["next_cursor"]
        if not cursor:
            break
        params["cursor"] = cursor
    assert len(rows) == 5 and len({(row["source"], row["reference"]) for row in rows}) == 5
    assert {row["source"] for row in rows} == {"HTTP", "OPERATION", "GENERATION", "SIP"}
    exported = await client.get(
        "/api/v1/admin/diagnostics/report", params={**params, "export": "true"}
    )
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "private, no-store"
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.json()["failures"]["items"] == rows
    assert exported.json()["failures"]["next_cursor"] is None
    assert await db.scalar(
        select(AuditLog.id).where(AuditLog.action == "TECHNICAL_REPORT_EXPORTED")
    )


async def test_empty_report_and_integrity_do_not_invent_usage(client: AsyncClient) -> None:
    await sign_in(client, "admin", "admin")
    response = await client.get(
        "/api/v1/admin/diagnostics/report",
        params={
            "from": "1900-01-01T00:00:00Z",
            "to": "1900-01-02T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert not any(value["usage"].values())
    assert value["failures"] == {"items": [], "total": 0, "next_cursor": None}
    assert len(value["integrity"]) == 6
    assert all(row["status"] == "OK" for row in value["integrity"]), value["integrity"]


async def test_export_refuses_truncation_and_bad_cursor(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(client, "admin", "admin")
    monkeypatch.setattr(diagnostics, "EXPORT_LIMIT", 1)
    start = datetime(2019, 1, 1, tzinfo=UTC)
    for _ in range(2):
        db.add(AuditLog(action="SYSTEM_HTTP_ERROR", created_at=start, payload={}))
    await db.flush()
    params = {"from": start.isoformat(), "to": (start + timedelta(days=1)).isoformat()}
    response = await client.get(
        "/api/v1/admin/diagnostics/report", params={**params, "export": "true"}
    )
    assert response.status_code == 400 and "10 000" in response.json()["error"]["message"]
    assert (
        await client.get(
            "/api/v1/admin/diagnostics/report", params={**params, "cursor": "bad-cursor"}
        )
    ).status_code == 400


@pytest.mark.parametrize(
    "statement,code",
    [
        ("ALTER TABLE audit_log DISABLE TRIGGER audit_immutable", "audit"),
        (
            "CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$",
            "audit",
        ),
        ("GRANT UPDATE ON audit_log TO dispatcher_app", "application_role"),
        ("GRANT UPDATE(payload) ON audit_log TO dispatcher_app", "application_role"),
        ("GRANT UPDATE ON audit_log TO dispatcher_backup", "backup_role"),
        ("GRANT UPDATE ON system_settings TO dispatcher_backup", "backup_role"),
        ("GRANT UPDATE(value) ON system_settings TO dispatcher_backup", "backup_role"),
        ("ALTER TABLE system_settings OWNER TO dispatcher_backup", "backup_role"),
        ("ALTER TABLE system_settings OWNER TO dispatcher_app", "application_role"),
        ("GRANT UPDATE ON SEQUENCE audit_log_id_seq TO dispatcher_backup", "backup_role"),
        ("UPDATE alembic_version SET version_num='unknown'", "schema"),
        ("ALTER TABLE users DROP CONSTRAINT users_service_id_fkey", "foreign_keys"),
        (
            "ALTER TABLE users ADD CONSTRAINT diagnostic_fk FOREIGN KEY (service_id) "
            "REFERENCES services(id) NOT VALID",
            "foreign_keys",
        ),
    ],
)
async def test_integrity_detects_actual_database_drift(
    db: AsyncSession, statement: str, code: str
) -> None:
    await db.execute(text(statement))
    result = {row["code"]: row["status"] for row in await diagnostics.integrity_checks(db)}
    assert result[code] == "FAIL"


async def test_unavailable_backup_is_visible_without_hiding_other_checks(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diagnostics,
        "catalog",
        AsyncMock(side_effect=APIError(503, "BACKUP_UNAVAILABLE", "Unavailable")),
    )
    result = await diagnostics.integrity_checks(db)
    assert result[-1] == {"code": "backup_catalog", "status": "UNAVAILABLE"}
    assert all(row["status"] == "OK" for row in result[:-1]), result


@pytest.mark.parametrize(
    "items,status",
    [
        ([], "EMPTY"),
        ([{"valid_manifest": False}], "FAIL"),
        ([{"valid_manifest": True}], "OK"),
    ],
)
async def test_backup_integrity_distinguishes_empty_invalid_and_valid_catalog(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]], status: str
) -> None:
    monkeypatch.setattr(diagnostics, "catalog", AsyncMock(return_value={"items": items}))
    assert (await diagnostics.integrity_checks(db))[-1]["status"] == status


async def test_http_failure_is_persisted_separately_without_request_contents(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def session() -> Any:
        yield db

    async def fail() -> None:
        raise APIError(503, "PROBE_UNAVAILABLE", "Unavailable")

    monkeypatch.setattr(diagnostics, "session_factory", session)
    monkeypatch.setattr(app.state, "capture_system_errors", True, raising=False)
    app.add_api_route("/diagnostics-probe/{value}", fail)
    route = app.routes[-1]
    try:
        response = await client.get(
            "/diagnostics-probe/private-teaching-text?token=private-password"
        )
    finally:
        app.routes.remove(route)
    assert response.status_code == 503
    row = (await db.scalars(select(AuditLog).where(AuditLog.action == "SYSTEM_HTTP_ERROR"))).one()
    assert row.payload == {
        "request_id": response.headers["x-request-id"],
        "method": "GET",
        "route": "/diagnostics-probe/{value}",
        "status": 503,
        "code": "PROBE_UNAVAILABLE",
    }
    assert row.user_id is None and "private-" not in json.dumps(row.payload)


async def test_journal_failure_is_counted_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable() -> Any:
        raise OSError("private-password")

    monkeypatch.setattr(diagnostics, "session_factory", unavailable)
    monkeypatch.setattr(metrics, "diagnostic_write_failures", 0)
    await diagnostics.save_http_failure({"code": "INTERNAL_ERROR"})
    assert metrics.diagnostic_write_failures == 1
    assert (
        "System error journal write failed" in caplog.text and "private-password" not in caplog.text
    )


def test_http_summary_is_explicitly_since_process_start_and_uses_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("counts", "duration_sum", "duration_count", "buckets"):
        monkeypatch.setattr(metrics, name, defaultdict(float if name == "duration_sum" else int))
    assert metrics.request_summary()["mean_ms"] is None
    metrics.record_request("GET", "/healthz", 200, 0.1)
    metrics.record_request("arbitrary-private-method", "unmatched", 503, 3.0)
    value = metrics.request_summary()
    assert value["requests"] == 2 and value["server_errors"] == 1
    assert value["mean_ms"] == 1550 and value["over_two_seconds"] == 1
    assert value["since"] == metrics.started_at.isoformat()
    assert "arbitrary-private-method" not in metrics.render_metrics()
