"""Restartable replacement of program files; caller must stop writers under update maintenance."""

import hmac
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.operations.backup_protocol import signature
from app.operations.program_files import PROGRAM_TREES, checked_source
from app.operations.snapshot import canonical, file_digest
from app.operations.update_package import (
    MAX_BYTES,
    MAX_FILES,
    package_path,
    release_files,
    unpack_package,
)

JOURNAL = "file-transaction.json"
PURPOSE = "software-file-transaction-1"


def write_record(
    path: Path, value: dict[str, Any], key: bytes, purpose: str, *, exclusive: bool = False
) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Signed update records cannot use symbolic links")
    data = canonical({"value": value, "signature": signature(value, key, purpose)})
    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o600)
        if exclusive:
            try:
                path.hardlink_to(Path(name))
            except FileExistsError:
                raise ValueError("The signed update record already exists") from None
        else:
            Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_record(
    path: Path, key: bytes, purpose: str, limit: int = 8 * 1024 * 1024
) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > limit:
        raise ValueError("Invalid signed update record")
    envelope = json.loads(path.read_bytes())
    if not isinstance(envelope, dict) or set(envelope) != {"value", "signature"}:
        raise ValueError("Invalid signed update record")
    value = envelope["value"]
    if (
        not isinstance(value, dict)
        or not isinstance(envelope["signature"], str)
        or not hmac.compare_digest(envelope["signature"], signature(value, key, purpose))
    ):
        raise ValueError("Invalid update record signature")
    return value


def checked_directories(root: Path, stage: Path) -> None:
    if (
        not root.is_absolute()
        or root.resolve() != root
        or stage.resolve() != stage
        or not stage.is_relative_to(root / ".updates")
        or stage == root / ".updates"
    ):
        raise ValueError("File transaction must stay in this installation's .updates")
    checked_source(stage, root)
    if not stage.is_dir():
        raise ValueError("The verified update directory is missing")


def inventory(root: Path) -> list[dict[str, Any]]:
    paths = release_files(root)
    if len(paths) > MAX_FILES:
        raise ValueError("Installed program exceeds the file count limit")
    result: list[dict[str, Any]] = [
        {
            "path": p.relative_to(root).as_posix(),
            "size": p.stat().st_size,
            "sha256": file_digest(p),
            "mode": p.stat().st_mode & 0o777,
        }
        for p in paths
    ]
    if sum(item["size"] for item in result) > MAX_BYTES:
        raise ValueError("Installed program exceeds the rollback size limit")
    return result


def verify_files(root: Path, files: list[dict[str, Any]]) -> None:
    for item in files:
        path = root / package_path(item["path"])
        checked_source(path, root)
        if (
            not path.is_file()
            or path.stat().st_size != item["size"]
            or file_digest(path) != item["sha256"]
        ):
            raise ValueError("Program or rollback files changed after verification")


def verify_tree(root: Path, files: list[dict[str, Any]]) -> None:
    verify_files(root, files)
    if {p.relative_to(root).as_posix() for p in release_files(root)} != {
        item["path"] for item in files
    }:
        raise ValueError("Unexpected files appeared during program replacement")


def copy_file(source: Path, destination: Path, root: Path, mode: int) -> None:
    checked_source(destination, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=destination.parent, suffix=".partial")
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        Path(name).replace(destination)
    finally:
        Path(name).unlink(missing_ok=True)


def prepare_files(root: Path, stage: Path, public_key: bytes, signing_key: bytes) -> dict[str, Any]:
    checked_directories(root, stage)
    if len(signing_key) < 32:
        raise ValueError("The installation signing key must contain at least 32 bytes")
    if (stage / JOURNAL).exists() or (stage / "previous").exists():
        raise ValueError("A file transaction has already been prepared")
    manifest = unpack_package(stage / "release.zip", public_key)
    after = manifest["files"]
    verify_files(stage / "program", after)
    if {p.relative_to(stage / "program").as_posix() for p in release_files(stage / "program")} != {
        item["path"] for item in after
    }:
        raise ValueError("The prepared release has unexpected files")
    before = inventory(root)
    if shutil.disk_usage(stage).free < sum(item["size"] for item in before) + 256 * 1024 * 1024:
        raise ValueError("Insufficient disk space for the rollback program")
    with tempfile.TemporaryDirectory(dir=stage) as temporary:
        previous = Path(temporary) / "previous"
        previous.mkdir()
        for tree in (*PROGRAM_TREES, "infra"):
            (previous / tree).mkdir(parents=True, exist_ok=True)
        for item in before:
            copy_file(root / item["path"], previous / item["path"], previous, item["mode"])
        verify_files(previous, before)
        if inventory(root) != before:
            raise ValueError("The installed program changed while preparing rollback")
        previous.rename(stage / "previous")
    value = {
        "schema": PURPOSE,
        "root": str(root),
        "phase": "PREPARED",
        "version": manifest["version"],
        "package_sha256": file_digest(stage / "release.zip"),
        "before": before,
        "after": after,
    }
    write_record(stage / JOURNAL, value, signing_key, PURPOSE)
    return value


def transaction(root: Path, stage: Path, key: bytes) -> dict[str, Any]:
    checked_directories(root, stage)
    value = read_record(stage / JOURNAL, key, PURPOSE)
    if value.get("schema") != PURPOSE or value.get("root") != str(root):
        raise ValueError("The file transaction belongs to another installation")
    for item in [*value["before"], *value["after"]]:
        package_path(item["path"])
        checked_source(root / item["path"], root)
    return value


def replace_files(
    root: Path, source: Path, desired: list[dict[str, Any]], obsolete: list[dict[str, Any]]
) -> None:
    for tree in (*PROGRAM_TREES, "infra"):
        checked_source(root / tree, root)
        (root / tree).mkdir(parents=True, exist_ok=True)
    for item in desired:
        copy_file(source / item["path"], root / item["path"], root, item.get("mode", 0o644))
    names = {item["path"] for item in desired}
    for item in obsolete:
        if item["path"] not in names:
            path = root / item["path"]
            checked_source(path, root)
            path.unlink(missing_ok=True)
    verify_tree(root, desired)


def apply_files(root: Path, stage: Path, key: bytes) -> dict[str, Any]:
    value = transaction(root, stage, key)
    if value["phase"] not in {"PREPARED", "APPLYING", "APPLIED"}:
        raise ValueError("This file transaction cannot be applied")
    verify_files(stage / "previous", value["before"])
    verify_files(stage / "program", value["after"])
    if value["phase"] == "APPLIED":
        verify_tree(root, value["after"])
        return value
    if value["phase"] == "PREPARED" and inventory(root) != value["before"]:
        raise ValueError("The installed program changed before replacement")
    value["phase"] = "APPLYING"
    write_record(stage / JOURNAL, value, key, PURPOSE)
    replace_files(root, stage / "program", value["after"], value["before"])
    value["phase"] = "APPLIED"
    write_record(stage / JOURNAL, value, key, PURPOSE)
    return value


def rollback_files(root: Path, stage: Path, key: bytes) -> dict[str, Any]:
    value = transaction(root, stage, key)
    if value["phase"] not in {"PREPARED", "APPLYING", "APPLIED", "ROLLING_BACK", "ROLLED_BACK"}:
        raise ValueError("This file transaction cannot be rolled back")
    verify_files(stage / "previous", value["before"])
    if value["phase"] == "ROLLED_BACK":
        verify_tree(root, value["before"])
        return value
    value["phase"] = "ROLLING_BACK"
    write_record(stage / JOURNAL, value, key, PURPOSE)
    replace_files(root, stage / "previous", value["before"], value["after"])
    value["phase"] = "ROLLED_BACK"
    write_record(stage / JOURNAL, value, key, PURPOSE)
    return value
