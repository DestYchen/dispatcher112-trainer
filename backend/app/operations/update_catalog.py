"""Local signed update archives and bounded requests for the installation operator."""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.operations.backup_protocol import read_signed, signature
from app.operations.program_files import checked_source
from app.operations.snapshot import file_digest
from app.operations.update_package import MAX_BYTES, MAX_MANIFEST_BYTES, VERSION, unpack_package

ROOT = Path("/data/materials/.software-updates")
PUBLISHER = Path("/run/software-publisher.pub")
TOKEN = Path("/run/backup-control/token")
PREFIX = "software_package:"
MAX_ARCHIVE = MAX_BYTES + 4 * MAX_MANIFEST_BYTES
MAX_PACKAGES = 20
MAX_STORAGE = 2 * 1024 * 1024 * 1024
REQUEST_PURPOSE = "software-update-request-1"
REQUEST_FIELDS = {
    "schema",
    "id",
    "actor_id",
    "package_id",
    "package_sha256",
    "publisher_sha256",
    "version",
    "reason",
    "created_at",
    "expires_at",
    "action",
    "update_id",
}


def publisher(path: Path | None = None) -> bytes | None:
    path = path or PUBLISHER
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Publisher key cannot be a link")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 1024:
        raise ValueError("Invalid publisher public key")
    value = path.read_bytes()
    if not isinstance(serialization.load_pem_public_key(value), Ed25519PublicKey):
        raise ValueError("An Ed25519 publisher public key is required")
    return value


def archive_path(identity: UUID) -> Path:
    path = ROOT / f"{identity}.zip"
    if ROOT.resolve() != ROOT:
        raise ValueError("Software package storage cannot use links")
    checked_source(path, ROOT)
    return path


def storage_bytes() -> int:
    if ROOT.resolve() != ROOT:
        raise ValueError("Software package storage cannot use links")
    total = 0
    for path in ROOT.iterdir() if ROOT.exists() else []:
        checked_source(path, ROOT)
        if not path.is_file():
            raise ValueError("Unexpected software package storage entry")
        total += path.stat().st_size
    return total


def verify_archive(path: Path, key: bytes) -> dict[str, Any]:
    manifest = unpack_package(path, key)
    return {
        "version": manifest["version"],
        "description": manifest["description"],
        "release_created_at": manifest["created_at"],
        "sha256": file_digest(path),
        "size": path.stat().st_size,
        "publisher_sha256": hashlib.sha256(key).hexdigest(),
        "program_files": len(manifest["files"]),
        "image_services": len(manifest["images"]),
    }


def validate_request(value: dict[str, Any], *, now: datetime | None = None) -> None:
    try:
        if set(value) != REQUEST_FIELDS or value["schema"] != REQUEST_PURPOSE:
            raise ValueError("Invalid software request fields")
        for name in ("id", "actor_id", "update_id"):
            if str(UUID(value[name])) != value[name]:
                raise ValueError("Invalid software request identity")
        if value["action"] not in {"apply", "activate", "rollback"}:
            raise ValueError("Unsupported software request action")
        if (
            value["package_id"] is not None
            and str(UUID(value["package_id"])) != value["package_id"]
        ):
            raise ValueError("Invalid software package identity")
        if value["action"] == "apply" and (
            value["package_id"] is None or value["update_id"] != value["id"]
        ):
            raise ValueError("A new update requires its own identity and a package")
        for name in ("package_sha256", "publisher_sha256"):
            if not isinstance(value[name], str) or not re.fullmatch(r"[a-f0-9]{64}", value[name]):
                raise ValueError("Invalid software request digest")
        if not isinstance(value["version"], str) or not VERSION.fullmatch(value["version"]):
            raise ValueError("Invalid software request version")
        if not isinstance(value["reason"], str) or not 5 <= len(value["reason"].strip()) <= 1000:
            raise ValueError("A software update reason is required")
        created = datetime.fromisoformat(value["created_at"])
        expires = datetime.fromisoformat(value["expires_at"])
        moment = now or datetime.now(UTC)
        if (
            created.tzinfo is None
            or expires.tzinfo is None
            or expires - created != timedelta(hours=24)
            or created > moment + timedelta(seconds=60)
            or expires <= moment
        ):
            raise ValueError("Software update request expired or has an invalid date")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Invalid software update request") from error


def signed_request(value: dict[str, Any], key: bytes) -> dict[str, Any]:
    validate_request(value)
    return {"value": value, "signature": signature(value, key, REQUEST_PURPOSE)}


def request_key(token: bytes, session_secret: str) -> bytes:
    # Recovery keeps the backup queue key but rotates JWT_SECRET. Bind to both.
    if (
        not isinstance(token, bytes)
        or len(token) < 32
        or not isinstance(session_secret, str)
        or len(session_secret) < 32
    ):
        raise ValueError("Installation signing keys are missing or invalid")
    return hmac.new(
        token, b"software-update-request-key-1:" + session_secret.encode("utf-8"), "sha256"
    ).digest()


def read_request(path: Path, key: bytes) -> dict[str, Any]:
    try:
        if path.stat().st_size > 8192:
            raise ValueError("Software update request is too large")
        value = read_signed(path, key, REQUEST_PURPOSE)
        validate_request(value)
        return value
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid signed software update request") from error
