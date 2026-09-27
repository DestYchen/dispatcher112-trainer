import asyncio
import subprocess
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import runtime_configuration, sip_worker
from app.api import sip
from app.db.models import AuditLog, DirectoryEntry, PhoneReport, Scenario, SipCall, User
from app.domain.card_entry import incoming_message
from app.domain.runtime_configuration import RuntimeConfiguration
from app.domain.sip import (
    control_password,
    endpoint_config,
    endpoint_name,
    endpoint_password,
    speech_name,
)
from app.realtime.clock import tick
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


@pytest.mark.parametrize(
    "answered,age,ended",
    [(False, 19, False), (False, 21, True), (True, 21, False), (True, 61, True)],
)
async def test_runtime_call_limits_end_only_expired_calls(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    answered: bool,
    age: int,
    ended: bool,
) -> None:
    _, assignments = await lesson_fixture(db, 1)
    assignment = assignments[0]
    value = RuntimeConfiguration()
    value.sip.inbound_ring_seconds = 10
    value.sip.unanswered_seconds = 20
    value.sip.max_call_seconds = 60
    monkeypatch.setattr(runtime_configuration, "current", value)
    now = datetime.now(UTC)
    row = SipCall(
        user_id=assignment.student_id,
        assignment_id=assignment.id,
        direction="INBOUND",
        media_name="0" * 64,
        state="CONNECTED" if answered else "RINGING",
        channel_id="timed-call",
        created_at=now - timedelta(seconds=age),
        answered_at=now - timedelta(seconds=age - 1) if answered else None,
    )
    db.add(row)
    await db.flush()
    original_state = assignment.state
    requests: list[str] = []

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json=[{"id": "timed-call"}] if request.method == "GET" else {})

    monkeypatch.setattr(sip_worker, "session_factory", factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await sip_worker.reconcile(client)
        await sip_worker.reconcile(client)
    assert (row.ended_at is not None) == ended
    assert requests.count("DELETE") == int(ended)
    assert assignment.state == original_state
    if ended:
        assert row.state == ("ENDED" if answered else "FAILED")
    assert await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "SIP_CALL_ENDED", AuditLog.entity_id == row.id)
    ) == int(ended)


async def test_runtime_inbound_ring_limit_is_sent_to_asterisk(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, assignments = await lesson_fixture(db, 1)
    assignment = assignments[0]
    value = RuntimeConfiguration()
    value.sip.inbound_ring_seconds = 17
    monkeypatch.setattr(runtime_configuration, "current", value)
    row = SipCall(
        user_id=assignment.student_id,
        assignment_id=assignment.id,
        direction="INBOUND",
        media_name="0" * 64,
        state="REQUESTED",
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[] if request.method == "GET" else {})

    monkeypatch.setattr(sip_worker, "session_factory", factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await sip_worker.reconcile(client)
    assert row.state == "RINGING"
    assert requests[-1].method == "POST"
    assert requests[-1].url.params["timeout"] == "17"


def test_credentials_have_distinct_purposes_and_change_with_password() -> None:
    user = uuid4()
    secret = "test-root-secret-" * 3
    password = endpoint_password(secret, user, "hash1")
    assert len(password) == 64
    assert password == endpoint_password(secret, user, "hash1")
    assert password != endpoint_password(secret, user, "hash2")
    assert password != endpoint_password(secret, uuid4(), "hash1")
    assert password != control_password(secret)
    config = endpoint_config(user, password)
    assert endpoint_name(user) in config and "max_contacts=1" in config
    assert "trunk" not in config and "hash1" not in config
    assert speech_name("Текст") == speech_name("Текст") != speech_name("Другой текст")


async def test_voice_acceptance_comes_only_from_authorized_sip_channel(
    client: httpx.AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lesson, assignments = await lesson_fixture(db, 1)
    lesson.settings = {
        **lesson.settings,
        "incoming_channel": "VOICE",
        "training_mode": "CARD_ENTRY",
    }
    assignment = assignments[0]
    assignment.task_mode = "CARD_ENTRY"
    await db.flush()
    assert lesson.started_at
    await tick(db, lesson.started_at)
    await sign_in(client)
    monkeypatch.setattr(sip, "ROOT", tmp_path)
    url = f"/api/v1/sip/assignments/{assignment.id}/calls"
    detail = (await client.get(f"/api/v1/student/assignments/{assignment.id}")).json()
    assert detail["entry"]["accepted_delay_ms"] is None
    assert detail["entry"]["incoming_message"] == "" and assignment.opened_at is None
    headers = {"Idempotency-Key": "sip-first"}
    unavailable = await client.post(url, json={"direction": "INBOUND"}, headers=headers)
    assert (
        unavailable.status_code == 409 and unavailable.json()["error"]["code"] == "VOICE_NOT_READY"
    )
    scenario = await db.get(Scenario, assignment.scenario_id)
    assert scenario
    name = speech_name(incoming_message(scenario.card_payload))
    (tmp_path / "speech").mkdir()
    (tmp_path / "speech" / f"{name}.wav").write_bytes(b"test prepared speech fixture")
    first = await client.post(url, json={"direction": "INBOUND"}, headers=headers)
    assert first.status_code == 200, first.text
    repeated = await client.post(url, json={"direction": "INBOUND"}, headers=headers)
    assert first.json() == repeated.json()
    busy = await client.post(
        url, json={"direction": "INBOUND"}, headers={"Idempotency-Key": "another"}
    )
    assert busy.json()["error"]["code"] == "PHONE_BUSY"
    row = (await db.scalars(select(SipCall).where(SipCall.assignment_id == assignment.id))).one()
    requests: list[tuple[str, str]] = []

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    async def notify(*args: Any) -> None:
        requests.append(("NOTIFY", str(args[-1])))

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    monkeypatch.setattr(sip_worker, "session_factory", factory)
    monkeypatch.setattr(sip_worker, "notify", notify)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as ari_client:
        event: dict[str, Any] = {
            "channel": {"id": "one", "name": "PJSIP/foreign-0001"},
            "args": [row.id.hex],
        }
        await sip_worker.connected(ari_client, event)
        assert assignment.opened_at is None and row.answered_at is None
        assert requests[-1] == ("DELETE", "/ari/channels/one")
        event["channel"]["name"] = f"PJSIP/{endpoint_name(assignment.student_id)}-0001"
        await sip_worker.connected(ari_client, event)
        answered = assignment.opened_at
        assert answered and answered == row.answered_at == assignment.primary_status_at
        assert row.state == "CONNECTED"
        await sip_worker.connected(ari_client, event)
        event["channel"]["id"] = "forged-replay"
        await sip_worker.connected(ari_client, event)
        assert assignment.opened_at == answered
        assert requests[-1] == ("DELETE", "/ari/channels/forged-replay")
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == assignment.id, AuditLog.action == "INCOMING_ACCEPTED")
        )
        == 1
    )
    await sip_worker.ended({"channel": {"id": "one"}})
    await sip_worker.ended({"channel": {"id": "one"}})
    assert row.state == "ENDED" and row.ended_at is not None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == row.id, AuditLog.action == "SIP_CALL_ENDED")
        )
        == 1
    )


@pytest.mark.parametrize(
    "role,password,status",
    [("student1", "student", 200), ("teacher", "teacher", 200), ("admin", "admin", 403)],
)
async def test_sip_credentials_only_for_own_training_identity(
    client: httpx.AsyncClient,
    role: str,
    password: str,
    status: int,
) -> None:
    await sign_in(client, role, password)
    response = await client.get("/api/v1/sip/session")
    assert response.status_code == status
    if status == 200:
        assert response.headers["Cache-Control"] == "no-store"
        assert len(response.json()["password"]) == 64


async def test_sip_ownership_queued_closed_and_only_directory_numbers(
    client: httpx.AsyncClient,
    db: AsyncSession,
) -> None:
    lesson, assignments = await lesson_fixture(db, 2)
    await sign_in(client)
    headers = {"Idempotency-Key": "dial"}
    prefix = "/api/v1/sip/assignments/"
    for assignment_id in (uuid4(), assignments[0].id):
        response = await client.post(
            f"{prefix}{assignment_id}/calls",
            json={"direction": "OUTBOUND", "number": "112"},
            headers=headers,
        )
        assert response.status_code == 404
    assert lesson.started_at
    await tick(db, lesson.started_at)
    assignment = assignments[0]
    await client.get(f"/api/v1/student/assignments/{assignment.id}")
    url = f"{prefix}{assignment.id}/calls"
    rejected = await client.post(
        url, json={"direction": "OUTBOUND", "number": "999999"}, headers=headers
    )
    assert rejected.status_code == 404
    wrong_mode = await client.post(url, json={"direction": "INBOUND"}, headers=headers)
    assert wrong_mode.status_code == 400
    student_id = assignment.student_id
    teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
    assignment.student_id = teacher.id
    await db.flush()
    assert (await client.get(url)).status_code == 404
    assignment.student_id = student_id
    assignment.state = "CLOSED"
    await db.flush()
    closed = await client.post(
        url, json={"direction": "OUTBOUND", "number": "2201"}, headers=headers
    )
    assert closed.json()["error"]["code"] == "CARD_CLOSED"


async def test_ringing_timeout_and_revoked_user_end_call(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, assignments = await lesson_fixture(db, 1)
    assignment = assignments[0]
    row = SipCall(
        user_id=assignment.student_id,
        assignment_id=assignment.id,
        direction="INBOUND",
        media_name="0" * 64,
        state="RINGING",
        channel_id="expired",
        created_at=datetime.now(UTC) - timedelta(seconds=61),
    )
    db.add(row)
    await db.flush()

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json=[{"id": "expired"}] if request.method == "GET" else {})

    monkeypatch.setattr(sip_worker, "session_factory", factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as ari_client:
        await sip_worker.reconcile(ari_client)
        assert row.state == "FAILED" and row.ended_at
        assert "DELETE" in requests
        row.state, row.ended_at = "CONNECTED", None
        row.answered_at = datetime.now(UTC)
        row.created_at = datetime.now(UTC)
        user = await db.get(User, assignment.student_id)
        assert user
        user.is_active = False
        await db.flush()
        await sip_worker.reconcile(ari_client)
        assert row.state == "ENDED" and row.ended_at


async def test_outgoing_report_requires_answer_and_uses_server_duration(
    client: httpx.AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lesson, assignments = await lesson_fixture(db, 1)
    assert lesson.started_at
    await tick(db, lesson.started_at)
    assignment = assignments[0]
    await sign_in(client)
    await client.get(f"/api/v1/student/assignments/{assignment.id}")
    directory = (await db.scalars(select(DirectoryEntry))).first()
    assert directory
    monkeypatch.setattr(sip, "ROOT", tmp_path)
    (tmp_path / "speech").mkdir()
    (tmp_path / "speech" / f"{speech_name(directory.greeting)}.wav").write_bytes(b"speech fixture")
    response = await client.post(
        f"/api/v1/sip/assignments/{assignment.id}/calls",
        json={"direction": "OUTBOUND", "number": directory.number},
        headers={"Idempotency-Key": "outgoing"},
    )
    assert response.status_code == 200, response.text
    row = (await db.scalars(select(SipCall).where(SipCall.assignment_id == assignment.id))).one()
    assert response.json()["call_id"] == str(row.id)
    payload = {
        "call_id": str(row.id),
        "transcript": "Учебный доклад о происшествии.",
        "duration_ms": 0,
    }
    report_url = f"/api/v1/student/assignments/{assignment.id}/report"
    before_answer = await client.post(
        report_url, json=payload, headers={"Idempotency-Key": "report"}
    )
    assert before_answer.json()["error"]["code"] == "CALL_NOT_ANSWERED"
    report = await db.get(PhoneReport, row.id)
    assert report and report.transcript is None
    row.answered_at = datetime.now(UTC) - timedelta(seconds=2)
    row.ended_at = row.answered_at + timedelta(seconds=1)
    row.state = "ENDED"
    await db.flush()
    saved = await client.post(report_url, json=payload, headers={"Idempotency-Key": "report"})
    assert saved.status_code == 200, saved.text
    assert report.duration_ms == 1000
    repeated = await client.post(report_url, json=payload, headers={"Idempotency-Key": "report"})
    assert repeated.json() == saved.json()


async def test_unanswered_call_cancel_is_owned_and_idempotent(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    _, assignments = await lesson_fixture(db, 1)
    row = SipCall(
        user_id=assignments[0].student_id,
        assignment_id=assignments[0].id,
        direction="OUTBOUND",
        media_name="0" * 64,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    await sign_in(client)
    url = f"/api/v1/sip/calls/{row.id}"
    assert (await client.get(url)).status_code == 200
    first = await client.post(url + "/cancel")
    assert first.status_code == 200 and row.state == "FAILED"
    assert (await client.post(url + "/cancel")).json() == first.json()
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == row.id, AuditLog.action == "SIP_CALL_CANCELLED")
        )
        == 1
    )
    assert (await client.post(f"/api/v1/sip/calls/{uuid4()}/cancel")).status_code == 404
    row.state, row.answered_at = "CONNECTED", datetime.now(UTC)
    await db.flush()
    assert (await client.post(url + "/cancel")).status_code == 409
    await client.post("/api/v1/auth/logout")
    await sign_in(client, "teacher", "teacher")
    assert (await client.get(url)).status_code == 403


@pytest.mark.parametrize("format", ["wav", "mp3"])
async def test_recording_access_is_student_and_own_teacher_only(
    client: httpx.AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    format: str,
) -> None:
    lesson, assignments = await lesson_fixture(db, 1)
    row = SipCall(
        user_id=assignments[0].student_id,
        assignment_id=assignments[0].id,
        direction="INBOUND",
        media_name="0" * 64,
        state="ENDED",
        created_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    monkeypatch.setattr(sip, "ROOT", tmp_path)
    (tmp_path / "recordings").mkdir()
    source = tmp_path / "recordings" / f"{row.id.hex}.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 8000)
    url = f"/api/v1/sip/calls/{row.id}/recording?format={format}"
    assert (await client.get(url)).status_code == 401
    await sign_in(client)
    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    if format == "wav":
        assert response.content == source.read_bytes()
    else:
        assert response.headers["content-type"] == "audio/mpeg"
        encoded, decoded = tmp_path / "encoded.mp3", tmp_path / "decoded.wav"
        encoded.write_bytes(response.content)
        await asyncio.to_thread(
            subprocess.run, ["lame", "--silent", "--decode", str(encoded), str(decoded)], check=True
        )
        with wave.open(str(decoded), "rb") as stream:
            assert stream.getnchannels() == 1 and stream.getframerate() == 8000
            assert stream.getnframes() >= 8000
    await client.post("/api/v1/auth/logout")
    await sign_in(client, "teacher", "teacher")
    assert (await client.get(url)).status_code == 200
    other = User(
        login="sip-other",
        password_hash="fixture",
        role="TEACHER",
        first_name="Другой",
        last_name="Преподаватель",
    )
    db.add(other)
    await db.flush()
    lesson.teacher_id = other.id
    await db.flush()
    assert (await client.get(url)).status_code == 404


async def test_mp3_invalid_audio_is_reported_without_leaking_a_file(
    client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, assignments = await lesson_fixture(db, 1)
    row = SipCall(
        user_id=assignments[0].student_id,
        assignment_id=assignments[0].id,
        direction="INBOUND",
        media_name="0" * 64,
        state="ENDED",
        created_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    monkeypatch.setattr(sip, "ROOT", tmp_path)
    (tmp_path / "recordings").mkdir()
    (tmp_path / "recordings" / f"{row.id.hex}.wav").write_bytes(b"invalid")
    await sign_in(client)
    response = await client.get(f"/api/v1/sip/calls/{row.id}/recording?format=mp3")
    assert response.status_code == 503 and response.json()["error"]["code"] == "PHONE_UNAVAILABLE"
    assert (await client.get(f"/api/v1/sip/calls/{row.id}/recording?format=zip")).status_code == 400
