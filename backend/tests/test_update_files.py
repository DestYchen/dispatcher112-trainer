import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.operations import update_files as files
from app.operations.recovery_compose import SERVICES
from app.operations.update_package import DEPLOYMENT_FILES, create_package, unpack_package
from tests.test_program_snapshots import program_fixture
from tests.test_update_package import signing_keys

KEY = b"test-only-installation-signing-key-32-bytes"


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path, bytes]:
    root = program_fixture(tmp_path / "installation").resolve()
    publisher = program_fixture(tmp_path / "publisher")
    for directory, version in ((root, b"old"), (publisher, b"new")):
        (directory / "infra").mkdir()
        (directory / "infra/service.conf").write_bytes(version)
        (directory / "backend/app/main.py").write_bytes(version)
        for name in DEPLOYMENT_FILES:
            (directory / name).write_bytes(b"services: {}")
    (root / "backend/app/obsolete.py").write_bytes(b"old-only module")
    (publisher / "backend/app/added.py").write_bytes(b"new-only module")
    (root / ".env").write_bytes(b"SECRET=kept")
    (root / "data").mkdir()
    (root / "data/material").write_bytes(b"teaching material")
    (root / ".secrets").mkdir()
    (root / ".secrets/key").write_bytes(b"private installation key")
    private, public = signing_keys()
    archive = tmp_path / "release.zip"
    create_package(
        publisher,
        archive,
        private,
        "2.0",
        "Updated software",
        dict.fromkeys(SERVICES, "sha256:" + "a" * 64),
    )
    stage = root / ".updates/release"
    stage.mkdir(parents=True)
    shutil.copyfile(archive, stage / "release.zip")
    unpack_package(archive, public, stage / "program")
    return root, stage, public


def private_files(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in (".env", "data/material", ".secrets/key")}


def test_apply_and_rollback_restore_program_exactly_without_touching_data(
    installation: tuple[Path, Path, bytes],
) -> None:
    root, stage, public = installation
    before, private = files.inventory(root), private_files(root)
    prepared = files.prepare_files(root, stage, public, KEY)
    assert prepared["phase"] == "PREPARED" and files.inventory(root) == before
    applied = files.apply_files(root, stage, KEY)
    assert applied["phase"] == "APPLIED"
    assert (root / "backend/app/main.py").read_bytes() == b"new"
    assert (root / "backend/app/added.py").is_file()
    assert not (root / "backend/app/obsolete.py").exists()
    assert files.apply_files(root, stage, KEY) == applied
    assert private_files(root) == private
    restored = files.rollback_files(root, stage, KEY)
    assert restored["phase"] == "ROLLED_BACK" and files.inventory(root) == before
    assert files.rollback_files(root, stage, KEY) == restored
    assert not (root / "backend/app/added.py").exists()
    assert private_files(root) == private
    with pytest.raises(ValueError, match="cannot be applied"):
        files.apply_files(root, stage, KEY)


@pytest.mark.parametrize("recover", ["resume", "rollback"])
def test_interrupted_replacement_can_resume_or_roll_back(
    installation: tuple[Path, Path, bytes], monkeypatch: pytest.MonkeyPatch, recover: str
) -> None:
    root, stage, public = installation
    before, private = files.inventory(root), private_files(root)
    files.prepare_files(root, stage, public, KEY)
    original = files.copy_file
    calls = 0

    def failed_copy(source: Path, destination: Path, target: Path, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("simulated power interruption")
        original(source, destination, target, mode)

    monkeypatch.setattr(files, "copy_file", failed_copy)
    with pytest.raises(OSError, match="power interruption"):
        files.apply_files(root, stage, KEY)
    assert files.transaction(root, stage, KEY)["phase"] == "APPLYING"
    monkeypatch.setattr(files, "copy_file", original)
    if recover == "resume":
        assert files.apply_files(root, stage, KEY)["phase"] == "APPLIED"
        assert (root / "backend/app/main.py").read_bytes() == b"new"
    else:
        assert files.rollback_files(root, stage, KEY)["phase"] == "ROLLED_BACK"
        assert files.inventory(root) == before
    assert private_files(root) == private


def test_rollback_interruption_is_resumable(
    installation: tuple[Path, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, stage, public = installation
    before = files.inventory(root)
    files.prepare_files(root, stage, public, KEY)
    files.apply_files(root, stage, KEY)
    original = files.copy_file
    calls = 0

    def failed_copy(source: Path, destination: Path, target: Path, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("interrupted rollback")
        original(source, destination, target, mode)

    monkeypatch.setattr(files, "copy_file", failed_copy)
    with pytest.raises(OSError):
        files.rollback_files(root, stage, KEY)
    assert files.transaction(root, stage, KEY)["phase"] == "ROLLING_BACK"
    monkeypatch.setattr(files, "copy_file", original)
    files.rollback_files(root, stage, KEY)
    assert files.inventory(root) == before


@pytest.mark.parametrize("changed", ["journal", "previous", "program", "installed"])
def test_tampering_prevents_replacement_before_any_file_is_written(
    installation: tuple[Path, Path, bytes], changed: str
) -> None:
    root, stage, public = installation
    files.prepare_files(root, stage, public, KEY)
    if changed == "journal":
        path = stage / files.JOURNAL
        envelope = json.loads(path.read_bytes())
        envelope["value"]["root"] = str(root.parent)
        path.write_text(json.dumps(envelope))
    else:
        directory = root if changed == "installed" else stage / changed
        (directory / "backend/app/main.py").write_bytes(b"unexpected change")
    before = files.inventory(root)
    with pytest.raises(ValueError):
        files.apply_files(root, stage, KEY)
    assert files.inventory(root) == before


def test_corrupt_rollback_copy_does_not_overwrite_working_program(
    installation: tuple[Path, Path, bytes],
) -> None:
    root, stage, public = installation
    files.prepare_files(root, stage, public, KEY)
    files.apply_files(root, stage, KEY)
    before = files.inventory(root)
    (stage / "previous/backend/app/main.py").write_bytes(b"bad backup")
    with pytest.raises(ValueError, match="changed"):
        files.rollback_files(root, stage, KEY)
    assert files.inventory(root) == before


@pytest.mark.parametrize("path", [".", ".updates", "data", "../outside", ".updates/../data"])
def test_transaction_paths_are_confined_to_the_installation(
    installation: tuple[Path, Path, bytes], path: str
) -> None:
    root, _, _ = installation
    with pytest.raises(ValueError):
        files.checked_directories(root, root / path)


def test_update_record_signature_binds_purpose_and_key(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    value: dict[str, Any] = {"phase": "STARTED", "reason": "Пример"}
    files.write_record(path, value, KEY, "test-purpose")
    assert files.read_record(path, KEY, "test-purpose") == value
    for key, purpose in (
        (KEY, "other"),
        (b"other-installation-key-at-least-32-bytes", "test-purpose"),
    ):
        with pytest.raises(ValueError, match="signature"):
            files.read_record(path, key, purpose)
    with pytest.raises(ValueError, match="record"):
        files.read_record(path, KEY, "test-purpose", limit=1)


def test_exclusive_record_creation_preserves_existing_evidence(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    files.write_record(path, {"first": True}, KEY, "audit", exclusive=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        files.write_record(path, {"first": False}, KEY, "audit", exclusive=True)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("phase", ["APPLIED", "ROLLED_BACK"])
def test_idempotent_recheck_does_not_ignore_added_code(
    installation: tuple[Path, Path, bytes], phase: str
) -> None:
    root, stage, public = installation
    files.prepare_files(root, stage, public, KEY)
    files.apply_files(root, stage, KEY)
    if phase == "ROLLED_BACK":
        files.rollback_files(root, stage, KEY)
    (root / "backend/app/extra.py").write_bytes(b"unexpected extra code")
    with pytest.raises(ValueError, match="Unexpected files"):
        if phase == "APPLIED":
            files.apply_files(root, stage, KEY)
        else:
            files.rollback_files(root, stage, KEY)
