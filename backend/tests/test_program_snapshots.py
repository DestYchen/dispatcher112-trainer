import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from app.operations import snapshot
from app.operations.program_files import PROGRAM_TREES, REQUIRED_PROGRAM_FILES, program_files
from tests.test_snapshots import fixture_snapshot


def program_fixture(root: Path) -> Path:
    for relative in PROGRAM_TREES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in REQUIRED_PROGRAM_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source fixture {relative}", encoding="utf-8")
    return root


def test_source_inventory_includes_assets_and_migrations_but_not_secrets_or_caches(
    tmp_path: Path,
) -> None:
    root = program_fixture(tmp_path / "program")
    included = [
        "frontend/public/fonts/local.woff2",
        "backend/alembic/versions/new.py",
        "scripts/requirements-tls.txt",
        "docs/DEPLOY.md",
    ]
    excluded = [
        "backend/app/__pycache__/secret.pyc",
        "backend/app/.env",
        "frontend/src/.env.local",
        "frontend/node_modules/library/index.js",
        "frontend/dist/index.html",
        "scripts/temporary.log",
        "scripts/x.partial",
        ".secrets/key",
        ".backups/private",
        "backend/.venv/private",
    ]
    for name in [*included, *excluded]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    paths = {path.relative_to(root).as_posix() for path in program_files(root)}
    assert paths == REQUIRED_PROGRAM_FILES | set(included)


@pytest.mark.parametrize(
    "missing",
    ["frontend/package-lock.json", "backend/app/main.py", "backend/requirements.lock", "docs"],
)
def test_missing_source_or_lockfile_fails_before_creating_snapshot(
    tmp_path: Path,
    missing: str,
) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    root = program_fixture(tmp_path / "program")
    path = root / missing
    path.rmdir() if path.is_dir() else path.unlink()
    with pytest.raises(ValueError, match="missing"):
        snapshot.create_snapshot(backups, data, config, database, key, program_root=root)
    assert not backups.exists()


@pytest.mark.parametrize("directory", [False, True])
def test_source_symlink_cannot_copy_an_external_file(tmp_path: Path, directory: bool) -> None:
    root = program_fixture(tmp_path / "program")
    outside = tmp_path / "private"
    outside.mkdir()
    secret = outside / "key"
    secret.write_bytes(b"must not be copied")
    try:
        (root / "scripts/link").symlink_to(
            outside if directory else secret, target_is_directory=directory
        )
    except OSError as error:
        pytest.skip(f"This platform does not allow test symlinks: {error}")
    with pytest.raises(ValueError, match="symbolic"):
        program_files(root)


def test_complete_program_roundtrip_and_legacy_restore(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    root = program_fixture(tmp_path / "program")
    legacy = snapshot.create_snapshot(backups, data, config, database, key)
    result = snapshot.create_snapshot(backups, data, config, database, key, program_root=root)
    assert result["schema"] == "dispatcher-backup-2"
    assert result["program_files"] == len(REQUIRED_PROGRAM_FILES)
    target = tmp_path / "restored"
    manifest = snapshot.export_snapshot(backups, result["name"], key, target)
    assert {item["scope"] for item in manifest["files"]} == {
        "program",
        "data",
        "config",
        "database",
    }
    for source in program_files(root):
        assert (target / "program" / source.relative_to(root)).read_bytes() == source.read_bytes()
    restored_legacy = snapshot.export_snapshot(backups, legacy["name"], key, tmp_path / "legacy")
    assert restored_legacy["schema"] == "dispatcher-backup-1"
    assert not (tmp_path / "legacy/program").exists()
    assert legacy["program_files"] == 0


@pytest.mark.parametrize("change", ["remove_entry", "legacy_program", "bad_version", "bad_dump"])
def test_even_signed_incomplete_or_unknown_program_manifest_is_rejected(
    tmp_path: Path,
    change: str,
) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    root = program_fixture(tmp_path / "program")
    result = snapshot.create_snapshot(backups, data, config, database, key, program_root=root)
    path = backups / "snapshots" / result["name"] / "manifest.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    manifest = envelope["manifest"]
    if change == "remove_entry":
        manifest["files"] = [
            item for item in manifest["files"] if item["path"] != "backend/app/main.py"
        ]
    elif change == "legacy_program":
        manifest["schema"] = "dispatcher-backup-1"
    elif change == "bad_version":
        manifest["schema"] = "dispatcher-backup-3"
    else:
        next(item for item in manifest["files"] if item["scope"] == "database")["path"] = (
            "other.dump"
        )
    envelope["hmac_sha256"] = hmac.new(
        key, snapshot.canonical(manifest), hashlib.sha256
    ).hexdigest()
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError):
        snapshot.export_snapshot(backups, result["name"], key, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_modified_program_object_prevents_restore(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    root = program_fixture(tmp_path / "program")
    result = snapshot.create_snapshot(backups, data, config, database, key, program_root=root)
    manifest = snapshot.read_manifest(backups, result["name"], key)
    item = next(item for item in manifest["files"] if item["scope"] == "program")
    (backups / "objects" / item["sha256"]).write_bytes(b"different code")
    with pytest.raises(ValueError, match="повреждён"):
        snapshot.export_snapshot(backups, result["name"], key, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("change", ["edit", "add", "delete"])
def test_changing_code_while_copying_never_publishes_a_partial_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    root = program_fixture(tmp_path / "program")
    sources = program_files(root)
    store_file = snapshot.store_file

    def changing_source(source: Path, objects: Path) -> dict[str, Any]:
        result = store_file(source, objects)
        if source == sources[-1]:
            if change == "edit":
                sources[0].write_bytes(b"new source revision")
            elif change == "add":
                (root / "scripts/new.py").write_bytes(b"new file")
            else:
                sources[0].unlink()
        return result

    monkeypatch.setattr(snapshot, "store_file", changing_source)
    with pytest.raises(ValueError):
        snapshot.create_snapshot(backups, data, config, database, key, program_root=root)
    assert not (backups / "manifest.json").exists()
    assert not list((backups / "snapshots").iterdir())


def test_retention_accepts_mixed_formats_without_losing_program_objects(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    root = program_fixture(tmp_path / "program")
    snapshot.create_snapshot(backups, data, config, database, key)
    current = snapshot.create_snapshot(backups, data, config, database, key, program_root=root)
    for _ in range(13):
        snapshot.create_snapshot(backups, data, config, database, key)
    assert len(list((backups / "snapshots").iterdir())) == 14
    assert (
        snapshot.verify_snapshot(backups, current["name"], key)["schema"] == "dispatcher-backup-2"
    )
