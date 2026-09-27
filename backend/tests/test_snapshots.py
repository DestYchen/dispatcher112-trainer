import json
from pathlib import Path

import pytest

from app.operations.snapshot import create_snapshot, export_snapshot, verify_snapshot


def fixture_snapshot(tmp_path: Path) -> tuple[Path, Path, Path, Path, bytes]:
    data, config, backups = tmp_path / "data", tmp_path / "config", tmp_path / "backups"
    data.mkdir()
    config.mkdir()
    (data / "material.txt").write_text("Учебный материал", encoding="utf-8")
    (config / ".env").write_text("SECRET=private-value", encoding="utf-8")
    database = tmp_path / "database.dump"
    database.write_bytes(b"database test fixture")
    return backups, data, config, database, b"signing-test-key"


def test_full_snapshot_deduplicates_and_restores_files_and_configuration(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    (config / ".secrets").mkdir()
    (config / ".secrets/backup-signing.key").write_bytes(key)
    one = create_snapshot(backups, data, config, database, key)
    two = create_snapshot(backups, data, config, database, key)
    assert len(list((backups / "objects").iterdir())) == 3
    target = tmp_path / "restore"
    manifest = export_snapshot(backups, one["name"], key, target)
    assert len(manifest["files"]) == 3
    assert not (target / "config/.secrets/backup-signing.key").exists()
    assert (target / "data/material.txt").read_bytes() == (data / "material.txt").read_bytes()
    assert (target / "config/.env").read_bytes() == (config / ".env").read_bytes()
    assert (target / "database/database.dump").read_bytes() == database.read_bytes()
    assert two["name"] != one["name"]
    with pytest.raises(ValueError):
        export_snapshot(backups, one["name"], key, target)


def test_signed_manifest_tampering_and_file_corruption_prevent_restore(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    result = create_snapshot(backups, data, config, database, key)
    path = backups / "snapshots" / result["name"] / "manifest.json"
    original = path.read_bytes()
    payload = json.loads(original)
    payload["manifest"]["files"][0]["path"] = "../../escape"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Подпись"):
        verify_snapshot(backups, result["name"], key)
    path.write_bytes(original)
    (backups / "objects" / payload["manifest"]["files"][0]["sha256"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="повреждён"):
        export_snapshot(backups, result["name"], key, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


def test_retention_keeps_fourteen_and_collects_only_unreferenced_objects(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    names = []
    for index in range(15):
        database.write_text(str(index), encoding="utf-8")
        names.append(create_snapshot(backups, data, config, database, key)["name"])
    assert len(list((backups / "snapshots").iterdir())) == 14
    assert not (backups / "snapshots" / names[0]).exists()
    assert len(list((backups / "objects").iterdir())) == 16
    for name in names[1:]:
        verify_snapshot(backups, name, key)


def test_backup_excludes_itself_and_rejects_symlinks(tmp_path: Path) -> None:
    backups, data, config, database, key = fixture_snapshot(tmp_path)
    backups = data / "backups"
    create_snapshot(backups, data, config, database, key)
    second = create_snapshot(backups, data, config, database, key)
    assert second["files"] == 3
    try:
        (data / "external").symlink_to(database)
    except OSError as error:
        pytest.skip(f"This platform does not allow test symlinks: {error}")
    with pytest.raises(ValueError, match="ссылка"):
        create_snapshot(backups, data, config, database, key)


@pytest.mark.parametrize("name", ["../escape", "/tmp/file", "backup-fake", "..\\escape"])
def test_unknown_snapshot_path_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        verify_snapshot(tmp_path, name, b"key")
