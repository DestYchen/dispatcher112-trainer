import hashlib
import json
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, DirectoryEntry, PhoneReport, Scenario
from app.realtime.clock import tick
from app.seeds.directory import seed_directory
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


def test_prebuilt_voices_are_distinct_valid_wavs_and_match_directory() -> None:
    from app.seeds.directory import ENTRIES

    root = Path("/data/voices")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    hashes = []
    for entry in ENTRIES:
        voice = manifest["voices"][entry["voice"]]
        assert (
            voice["greeting"] == entry["greeting"]
            and voice["confirmation"] == entry["confirmation"]
        )
        for phrase in ("greeting", "confirm"):
            path = root / entry["voice"] / (phrase + ".wav")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == voice["files"][phrase]
            hashes.append(digest)
            with wave.open(str(path)) as audio:
                assert (
                    audio.getframerate() == 24000
                    and audio.getsampwidth() == 2
                    and audio.getnchannels() == 1
                )
                assert 0.5 < audio.getnframes() / audio.getframerate() < 10
                assert len(set(audio.readframes(audio.getnframes()))) > 100
    assert len(set(hashes)) == 8


async def test_voice_is_served_as_local_wav(client: AsyncClient) -> None:
    response = await client.get("/media/voices/male_calm/greeting.wav")
    assert response.status_code == 200 and response.content[:4] == b"RIFF"
    assert response.headers["content-type"] in {"audio/wav", "audio/x-wav", "audio/vnd.wave"}


async def test_four_directory_entries_are_idempotent_and_have_distinct_voices(
    db: AsyncSession, client: AsyncClient
) -> None:
    await seed_directory(db)
    await seed_directory(db)
    entries = list(await db.scalars(select(DirectoryEntry)))
    assert len(entries) == 4 and len({row.voice for row in entries}) == 4
    assert {row.number for row in entries} == {"2201", "2214", "112", "2210"}
    await sign_in(client)
    response = await client.get("/api/v1/student/directory")
    assert response.status_code == 200 and len(response.json()["entries"]) == 4


async def test_call_report_server_duration_idempotency_and_completeness(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    lesson.settings = {**lesson.settings, "grammar_check_enabled": False}
    scenario = await db.get(Scenario, rows[0].scenario_id)
    assert scenario
    scenario.reference = {
        **scenario.reference,
        "report_required": True,
        "report_callee_code": "DUTY_OFFICER",
        "report_must_mention": ["address", "incident_type", "victims"],
    }
    await tick(db, datetime.now(UTC))
    await sign_in(client)
    prefix = f"/api/v1/student/assignments/{rows[0].id}"
    assert (await client.post(prefix + "/call", json={"number": "2201"})).status_code == 409
    await client.get(prefix)
    unknown = await client.post(prefix + "/call", json={"number": "999"})
    assert unknown.status_code == 404 and "не отвечает" in unknown.text
    assert not list(await db.scalars(select(PhoneReport)))
    call = await client.post(prefix + "/call", json={"number": "2201"})
    assert call.status_code == 200 and call.json()["greeting_audio_url"].endswith(
        "male_calm/greeting.wav"
    )
    row = (await db.scalars(select(PhoneReport))).one()
    row.created_at = datetime.now(UTC) - timedelta(seconds=10)
    await db.flush()
    body = {
        "call_id": call.json()["call_id"],
        "transcript": "Пожар, ул. Дубнинская, д. 28. Пострадавших нет. Бригада направлена.",
        "duration_ms": 1,
    }
    headers = {"Idempotency-Key": str(uuid4())}
    first = await client.post(prefix + "/report", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert row.duration_ms is not None and 10000 <= row.duration_ms < 12000
    second = await client.post(prefix + "/report", json=body, headers=headers)
    assert first.json() == second.json()
    assert (
        await client.post(
            prefix + "/report", json={**body, "transcript": "Изменено"}, headers=headers
        )
    ).status_code == 400
    assert (
        await client.post(prefix + "/report", json=body, headers={"Idempotency-Key": str(uuid4())})
    ).status_code == 409
    for status in ("ACCEPTED", "WORK_COMPLETED"):
        assert (
            await client.post(
                prefix + "/status",
                json={"status": status},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 200
    assert rows[0].score and rows[0].score["axes"]["completeness"]["score"] == 100
    assert await db.scalar(select(func.count()).select_from(PhoneReport)) == 1
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "PHONE_REPORT")
        )
        == 1
    )
    assert (await client.post(prefix + "/call", json={"number": "2201"})).status_code == 409
    assert (
        await client.post(prefix + "/report", json=body, headers=headers)
    ).json() == first.json()


async def test_report_cannot_use_another_card_call_or_empty_transcript(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, rows = await lesson_fixture(db, 2)
    now = datetime.now(UTC)
    await tick(db, now)
    await tick(db, now + timedelta(seconds=6))
    await sign_in(client)
    for row in rows:
        await client.get(f"/api/v1/student/assignments/{row.id}")
    call = await client.post(
        f"/api/v1/student/assignments/{rows[0].id}/call", json={"number": "112"}
    )
    body = {
        "call_id": call.json()["call_id"],
        "transcript": "Пожар. Пострадавших нет.",
        "duration_ms": 1000,
    }
    headers = {"Idempotency-Key": str(uuid4())}
    assert (
        await client.post(
            f"/api/v1/student/assignments/{rows[1].id}/report", json=body, headers=headers
        )
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/student/assignments/{uuid4()}/call", json={"number": "112"})
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/student/assignments/{rows[0].id}/report",
            json={**body, "transcript": "  "},
            headers=headers,
        )
    ).status_code == 400
    assert (
        await client.post(f"/api/v1/student/assignments/{rows[0].id}/report", json=body)
    ).status_code == 400
