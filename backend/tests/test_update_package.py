import base64
import json
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.operations import update_package as updates
from app.operations.program_files import PROGRAM_TREES
from app.operations.recovery_compose import SERVICES
from app.operations.snapshot import canonical
from tests.test_program_snapshots import program_fixture


def signing_keys() -> tuple[bytes, bytes]:
    key = Ed25519PrivateKey.generate()
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, Path, bytes, bytes, dict[str, Any]]:
    root = program_fixture(tmp_path / "source")
    (root / "infra").mkdir()
    (root / "infra/service.conf").write_bytes(b"configuration template")
    for name in updates.DEPLOYMENT_FILES:
        (root / name).write_bytes(b"services: {}")
    (root / ".env").write_bytes(b"PRIVATE=never-in-the-package")
    private, public = signing_keys()
    archive = tmp_path / "release.zip"
    manifest = updates.create_package(
        root,
        archive,
        private,
        "1.2.3",
        "Test release",
        dict.fromkeys(SERVICES, "sha256:" + "a" * 64),
    )
    return root, archive, private, public, manifest


Release = tuple[Path, Path, bytes, bytes, dict[str, Any]]


def rewrite(
    release: Release,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    payload: tuple[str, bytes] | None = None,
    extra: str | None = None,
    resign: bool = True,
    compressed: bool = False,
    symlink: bool = False,
) -> None:
    _, archive, private, _, _ = release
    with zipfile.ZipFile(archive) as package:
        contents = {entry.filename: package.read(entry) for entry in package.infolist()}
    envelope = json.loads(contents["release.json"])
    if mutate is not None:
        mutate(envelope["manifest"])
    if resign:
        key = serialization.load_pem_private_key(private, password=None)
        assert isinstance(key, Ed25519PrivateKey)
        envelope["signature"] = base64.b64encode(key.sign(canonical(envelope["manifest"]))).decode()
    contents["release.json"] = canonical(envelope)
    if payload is not None:
        contents[payload[0]] = payload[1]
    with zipfile.ZipFile(archive, "w") as package:
        for name, data in contents.items():
            entry = zipfile.ZipInfo(name)
            if compressed and name != "release.json":
                entry.compress_type = zipfile.ZIP_DEFLATED
            if symlink and name == "program/backend/app/main.py":
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(entry, data)
        if extra is not None:
            package.writestr(extra, b"unsigned")


def test_roundtrip_verifies_all_files_and_leaves_secrets_out(
    release: Release, tmp_path: Path
) -> None:
    root, archive, _, public, manifest = release
    destination = tmp_path / "checked"
    assert updates.unpack_package(archive, public, destination) == manifest
    assert all((destination / tree).is_dir() for tree in PROGRAM_TREES)
    assert {
        p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()
    } == {item["path"] for item in manifest["files"]}
    for item in manifest["files"]:
        assert (destination / item["path"]).read_bytes() == (root / item["path"]).read_bytes()
    assert not (destination / ".env").exists()
    assert not archive.with_name(archive.name + ".partial").exists()


def test_verification_without_extraction_creates_no_files(release: Release, tmp_path: Path) -> None:
    _, archive, _, public, manifest = release
    before = set(tmp_path.rglob("*"))
    assert updates.unpack_package(archive, public) == manifest
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("wrong_key", [False, True])
def test_untrusted_or_modified_signature_cannot_publish(
    release: Release, tmp_path: Path, wrong_key: bool
) -> None:
    _, archive, _, public, _ = release
    if wrong_key:
        _, public = signing_keys()
    else:
        rewrite(release, lambda m: m.update(version="other"), resign=False)
    with pytest.raises(ValueError, match="Подпись"):
        updates.unpack_package(archive, public, tmp_path / "checked")
    assert not (tmp_path / "checked").exists()


def test_changed_bytes_of_correct_length_do_not_publish_partial_tree(
    release: Release, tmp_path: Path
) -> None:
    _, archive, _, public, manifest = release
    item = manifest["files"][-1]
    rewrite(release, payload=("program/" + item["path"], b"x" * item["size"]))
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="Содержимое"):
        updates.unpack_package(archive, public, tmp_path / "checked")
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "C:/outside",
        "backend\\app\\test.py",
        "data/file",
        ".env",
        ".secrets/private",
        "backend/app/.env",
        "backend/app/.ENV.LOCAL",
        "backend/app/CON.py",
        "backend/app/LPT9.txt",
        "backend/app/dot.",
        "backend/app/space ",
        "backend/app/file:stream",
        "backend/app/private.key",
        "backend/app/a?.py",
        "backend/app/a*.py",
        "backend/app/a|b.py",
        "backend/app/a\x00.py",
        "backend/app/__pycache__/secret.py",
        "backend/app/.git/config",
        "backend/app/../file",
    ],
)
def test_package_rejects_paths_that_are_unsafe_or_not_program_files(name: str) -> None:
    with pytest.raises(ValueError):
        updates.package_path(name)


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["files"].append({**m["files"][0], "path": m["files"][0]["path"].upper()}),
        lambda m: m["files"].pop(0),
        lambda m: m["files"][0].update(size=True),
        lambda m: m["files"][0].update(size=-1),
        lambda m: m["files"][0].update(size=updates.MAX_BYTES + 1),
        lambda m: m["files"][0].update(sha256="not-a-digest"),
        lambda m: m["images"].update(backend="backend:latest"),
        lambda m: m["images"].pop("backend"),
        lambda m: m.update(created_at="2026-09-26T00:00:00"),
        lambda m: m.update(created_at=42),
        lambda m: m.update(version="../version"),
        lambda m: m.update(schema="unknown"),
        lambda m: m.update(command="arbitrary command"),
        lambda m: m.update(description=""),
        lambda m: m.update(files=[]),
    ],
)
def test_even_signed_invalid_manifests_are_rejected_before_extraction(
    release: Release, tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    _, archive, _, public, _ = release
    rewrite(release, change)
    with pytest.raises(ValueError):
        updates.unpack_package(archive, public, tmp_path / "checked")
    assert not (tmp_path / "checked").exists()


@pytest.mark.parametrize("invalid", ["unsigned", "duplicate", "compressed", "symlink", "size"])
def test_invalid_zip_structure_is_rejected(release: Release, invalid: str) -> None:
    _, archive, _, public, _ = release
    if invalid == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate"):
            rewrite(release, extra="release.json")
    else:
        rewrite(
            release,
            extra="program/unknown.py" if invalid == "unsigned" else None,
            compressed=invalid == "compressed",
            symlink=invalid == "symlink",
            payload=("program/backend/app/main.py", b"x") if invalid == "size" else None,
        )
    with pytest.raises(ValueError):
        updates.unpack_package(archive, public)


def test_existing_package_and_destination_are_never_overwritten(
    release: Release, tmp_path: Path
) -> None:
    root, archive, private, public, manifest = release
    original = archive.read_bytes()
    with pytest.raises(ValueError, match="существует"):
        updates.create_package(root, archive, private, "1.2.4", "New release", manifest["images"])
    assert archive.read_bytes() == original
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "private"
    sentinel.write_bytes(b"original")
    with pytest.raises(ValueError, match="новый каталог"):
        updates.unpack_package(archive, public, destination)
    assert sentinel.read_bytes() == b"original"


def test_source_change_during_build_discards_package(
    release: Release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, private, _, manifest = release
    original_write = zipfile.ZipFile.write

    def changed_write(self: zipfile.ZipFile, filename: Path, arcname: str) -> None:
        original_write(self, filename, arcname)
        if arcname == "program/backend/app/main.py":
            filename.write_bytes(b"changed during release")

    monkeypatch.setattr(zipfile.ZipFile, "write", changed_write)
    destination = tmp_path / "changed.zip"
    with pytest.raises(ValueError, match="Исходники изменились"):
        updates.create_package(
            root, destination, private, "1.2.4", "New release", manifest["images"]
        )
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".partial").exists()


@pytest.mark.parametrize("content", [b'{"manifest":{},"manifest":{},"signature":""}', b"[]"])
def test_ambiguous_or_nonobject_envelope_rejected(release: Release, content: bytes) -> None:
    _, archive, _, public, _ = release
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("release.json", content)
    with pytest.raises(ValueError):
        updates.unpack_package(archive, public)


def test_invalid_signed_manifest_type_is_rejected(release: Release) -> None:
    _, archive, private, public, _ = release
    key = serialization.load_pem_private_key(private, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "release.json",
            canonical(
                {
                    "manifest": None,
                    "signature": base64.b64encode(key.sign(b"null")).decode(),
                }
            ),
        )
    with pytest.raises(ValueError, match="формат"):
        updates.unpack_package(archive, public)
