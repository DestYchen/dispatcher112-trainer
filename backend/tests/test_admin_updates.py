import asyncio
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AuditLog, SystemSetting, User
from app.operations import update_catalog as catalog
from app.operations import update_database as updates
from app.operations.backup_protocol import signature
from tests.test_auth import sign_in
from tests.test_update_package import release as release
from tests.test_update_package import signing_keys

BASE = "/api/v1/admin/operations/updates"


@pytest.fixture
def archive(
    release: tuple[Path, Path, bytes, bytes, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    _, path, _, public, _ = release
    key = tmp_path / "publisher.pub"
    key.write_bytes(public)
    token = tmp_path / "token"
    token.write_bytes(b"software-request-test-token-32-bytes")
    monkeypatch.setattr(catalog, "ROOT", tmp_path / "catalog")
    monkeypatch.setattr(catalog, "PUBLISHER", key)
    monkeypatch.setattr(catalog, "TOKEN", token)
    return path


async def upload(client: AsyncClient, archive: Path) -> dict[str, Any]:
    response = await client.post(
        BASE + "/packages?filename=release.zip",
        content=(await asyncio.to_thread(archive.read_bytes)),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_verified_upload_download_request_replay_and_history(
    client: AsyncClient, db: AsyncSession, archive: Path
) -> None:
    await sign_in(client, "admin", "admin")
    db.add(SystemSetting(key="softwareXpackage:unrelated", value={"unrelated": True}))
    await db.commit()
    empty = (await client.get(BASE)).json()
    assert empty["packages"] == [] and empty["updates"] == []
    assert empty["trust"]["configured"] and empty["execution"] == "LOCAL_COMMAND"
    value = await upload(client, archive)
    assert (
        value["sha256"] == hashlib.sha256(await asyncio.to_thread(archive.read_bytes)).hexdigest()
    )
    assert await upload(client, archive) == value
    assert len((await client.get(BASE)).json()["packages"]) == 1
    assert (await client.get(BASE + "/packages/" + value["id"])).content == (
        await asyncio.to_thread(archive.read_bytes)
    )
    identity = str(uuid4())
    body = {
        "id": identity,
        "action": "apply",
        "package_id": value["id"],
        "reason": "Плановое обновление",
    }
    first = await client.post(BASE + "/requests", json=body)
    assert first.status_code == 200, first.text
    envelope = first.json()["request"]
    catalog.validate_request(envelope["value"])
    assert envelope["signature"] == signature(
        envelope["value"],
        catalog.request_key(catalog.TOKEN.read_bytes(), settings.jwt_secret),
        catalog.REQUEST_PURPOSE,
    )
    assert envelope["value"]["update_id"] == identity
    assert (await client.post(BASE + "/requests", json=body)).json() == first.json()
    conflicting = await client.post(
        BASE + "/requests", json={**body, "reason": "Другая причина обновления"}
    )
    assert (
        conflicting.status_code == 409
        and conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert (await client.delete(BASE + "/packages/" + value["id"])).status_code == 409
    history = (await client.get(BASE)).json()["updates"]
    assert history[0]["id"] == identity and history[0]["phase"] == "REQUESTED"
    catalog.PUBLISHER.write_bytes(signing_keys()[1])
    rotated = await client.post(BASE + "/requests", json=body)
    assert rotated.status_code == 409 and rotated.json()["error"]["code"] == "UPDATE_KEY_CHANGED"
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "SOFTWARE_UPDATE_REQUESTED")
        )
        == 1
    )


@pytest.mark.parametrize("role", ["teacher", "student1"])
async def test_teaching_roles_cannot_read_or_change_software(
    client: AsyncClient, archive: Path, role: str
) -> None:
    await sign_in(client, role, "teacher" if role == "teacher" else "student")
    for method, path, body in [
        ("GET", BASE, None),
        ("POST", BASE + "/packages?filename=release.zip", None),
        ("DELETE", BASE + f"/packages/{uuid4()}", None),
        (
            "POST",
            BASE + "/requests",
            {"id": str(uuid4()), "action": "apply", "reason": "Проверка роли"},
        ),
    ]:
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, response.text
    assert not catalog.ROOT.exists()


@pytest.mark.parametrize("failure", ["missing", "malformed", "other", "damaged", "empty"])
async def test_untrusted_or_invalid_package_is_never_published(
    client: AsyncClient, db: AsyncSession, archive: Path, failure: str
) -> None:
    await sign_in(client, "admin", "admin")
    if failure == "missing":
        catalog.PUBLISHER.unlink()
    elif failure == "malformed":
        catalog.PUBLISHER.write_bytes(b"invalid public key")
    elif failure == "other":
        catalog.PUBLISHER.write_bytes(signing_keys()[1])
    elif failure == "damaged":
        await asyncio.to_thread(archive.write_bytes, b"invalid ZIP")
    else:
        await asyncio.to_thread(archive.write_bytes, b"")
    response = await client.post(
        BASE + "/packages?filename=release.zip",
        content=(await asyncio.to_thread(archive.read_bytes)),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code in {400, 409, 503}
    assert (await client.get(BASE)).json()["packages"] == []
    assert not list(catalog.ROOT.glob("*"))
    if failure in {"other", "damaged", "empty"}:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "SOFTWARE_UPDATE_PACKAGE_REJECTED")
            )
            == 1
        )


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE", "MAX_STORAGE", "MAX_PACKAGES"])
async def test_package_limits_leave_no_partial_file(
    client: AsyncClient, archive: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    await sign_in(client, "admin", "admin")
    monkeypatch.setattr(catalog, limit, 0 if limit == "MAX_PACKAGES" else 1)
    response = await client.post(
        BASE + "/packages?filename=release.zip",
        content=(await asyncio.to_thread(archive.read_bytes)),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code in {409, 413}
    assert not list(catalog.ROOT.glob("*"))


async def test_changed_package_cannot_be_requested_or_downloaded(
    client: AsyncClient, archive: Path
) -> None:
    await sign_in(client, "admin", "admin")
    value = await upload(client, archive)
    catalog.archive_path(UUID(value["id"])).write_bytes(b"changed after verification")
    body = {
        "id": str(uuid4()),
        "action": "apply",
        "package_id": value["id"],
        "reason": "Проверка целостности",
    }
    assert (await client.post(BASE + "/requests", json=body)).status_code == 409
    assert (await client.get(BASE + "/packages/" + value["id"])).status_code == 409
    assert (await client.get(BASE)).json()["updates"] == []
    assert (await client.delete(BASE + "/packages/" + value["id"])).status_code == 204
    assert not catalog.archive_path(UUID(value["id"])).exists()


async def test_ready_update_allows_signed_completion_but_not_new_upload(
    client: AsyncClient, db: AsyncSession, archive: Path
) -> None:
    await sign_in(client, "admin", "admin")
    actor = await db.scalar(select(User.id).where(User.login == "admin"))
    assert actor is not None
    identity = uuid4()
    value = await updates.begin_update(
        db, identity, actor, "1.2.3", "a" * 64, "Проверка завершения"
    )
    value["phase"] = "READY"
    await updates.save_setting(db, "software_update", value, actor)
    await db.commit()
    for action in ("activate", "rollback"):
        response = await client.post(
            BASE + "/requests",
            json={
                "id": str(uuid4()),
                "action": action,
                "update_id": str(identity),
                "reason": "Решение администратора",
            },
        )
        assert response.status_code == 200, response.text
        catalog.validate_request(response.json()["request"]["value"])
    response = await client.post(
        BASE + "/packages?filename=release.zip",
        content=(await asyncio.to_thread(archive.read_bytes)),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 409
    assert (
        await client.post(
            BASE + "/requests",
            json={
                "id": str(uuid4()),
                "action": "activate",
                "update_id": str(uuid4()),
                "reason": "Чужое обновление",
            },
        )
    ).status_code == 409


def request_value() -> dict[str, Any]:
    now = datetime.now(UTC)
    identity = str(uuid4())
    return {
        "schema": catalog.REQUEST_PURPOSE,
        "id": identity,
        "update_id": identity,
        "action": "apply",
        "actor_id": str(uuid4()),
        "package_id": str(uuid4()),
        "package_sha256": "a" * 64,
        "publisher_sha256": "b" * 64,
        "version": "1.2.3",
        "reason": "Проверка подписи",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "other"),
        ("id", "../outside"),
        ("actor_id", "invalid"),
        ("update_id", str(uuid4())),
        ("action", "shell"),
        ("package_id", None),
        ("package_sha256", "sha256"),
        ("publisher_sha256", 42),
        ("version", "../bad"),
        ("reason", ""),
        ("created_at", "2020-01-01T00:00:00"),
        ("expires_at", "2020-01-01T00:00:00+00:00"),
    ],
)
def test_request_schema_rejects_unbounded_or_expired_commands(field: str, value: Any) -> None:
    request = request_value()
    request[field] = value
    with pytest.raises(ValueError):
        catalog.validate_request(request)


def test_downloaded_request_has_installation_signature_and_detects_tampering(
    tmp_path: Path,
) -> None:
    key = b"installation-test-signing-key-32-bytes"
    value = request_value()
    path = tmp_path / "request.json"
    envelope = catalog.signed_request(value, key)
    path.write_text(json.dumps(envelope))
    assert catalog.read_request(path, key) == value
    changed = copy.deepcopy(envelope)
    changed["value"]["reason"] = "Подменённое действие"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        catalog.read_request(path, key)
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        catalog.read_request(path, b"another-installation-signing-key-32-bytes")


def test_restored_instance_has_a_distinct_request_key_even_with_copied_backup_token(
    tmp_path: Path,
) -> None:
    token = b"shared-by-original-and-restored-backup-token"
    original = catalog.request_key(token, "original-instance-session-secret-32-bytes")
    recovered = catalog.request_key(token, "recovered-instance-session-secret-32-bytes")
    assert len(original) == len(recovered) == 32 and original != recovered
    value = request_value()
    path = tmp_path / "request.json"
    path.write_text(json.dumps(catalog.signed_request(value, original)))
    assert catalog.read_request(path, original) == value
    with pytest.raises(ValueError):
        catalog.read_request(path, recovered)
    with pytest.raises(ValueError):
        catalog.read_request(path, token)


@pytest.mark.parametrize("token,secret", [(b"short", "a" * 32), (b"a" * 32, "short")])
def test_request_key_refuses_missing_installation_secrets(token: bytes, secret: str) -> None:
    with pytest.raises(ValueError):
        catalog.request_key(token, secret)
