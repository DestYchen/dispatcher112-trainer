"""Compare installed program, configuration and static resources with an approved snapshot."""

import hashlib
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.operations.backup_protocol import read_signed, write_signed
from app.operations.program_files import checked_source, program_files
from app.operations.snapshot import SNAPSHOT_NAME, checked_relative, read_manifest

BASELINE = "integrity-baseline.json"
PURPOSE = "installation-integrity-baseline-1"
STATIC_TREES = ("fonts", "llm", "tickets", "voices")
STATIC_FILES = ("classifier.xlsx", "streets.csv")
MAX_FILES = 20000
MAX_BYTES = 64 * 1024**3
MAX_ISSUES = 200


def baseline(backups: Path, key: bytes) -> dict[str, Any] | None:
    path = backups / BASELINE
    if not path.exists() and not path.is_symlink():
        return None
    value = read_signed(path, key, PURPOSE)
    if (
        set(value) != {"snapshot", "id", "actor_id", "reason", "selected_at"}
        or not isinstance(value["snapshot"], str)
        or not SNAPSHOT_NAME.fullmatch(value["snapshot"])
        or str(UUID(value["id"])) != value["id"]
        or str(UUID(value["actor_id"])) != value["actor_id"]
        or not isinstance(value["reason"], str)
        or not 5 <= len(value["reason"].strip()) <= 1000
        or datetime.fromisoformat(value["selected_at"]).tzinfo is None
    ):
        raise ValueError("Invalid integrity baseline")
    return value


def included(scope: str, name: str) -> bool:
    relative = checked_relative(name)
    if scope in {"program", "config"}:
        return True
    return scope == "data" and (relative.parts[0] in STATIC_TREES or name in STATIC_FILES)


def safe_file(path: Path, root: Path) -> None:
    checked_source(path, root)
    for part in (path, *path.parents):
        attributes = part.lstat()
        if getattr(attributes, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise ValueError("Reparse points are not allowed")
        if part == root:
            break
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected a regular file")


def tree_files(directory: Path, root: Path) -> list[Path]:
    checked_source(directory, root)
    if not directory.exists():
        return []
    for part in (directory, *directory.parents):
        if getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Reparse points are not allowed")
        if part == root:
            break
    result = []

    def failed(error: OSError) -> None:
        raise error

    for current, directories, names in os.walk(directory, followlinks=False, onerror=failed):
        for name in [*directories, *names]:
            path = Path(current) / name
            checked_source(path, root)
            if getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("Reparse points are not allowed")
        for name in names:
            result.append(Path(current) / name)
            if len(result) > MAX_FILES:
                raise ValueError("Inventory limit exceeded")
    return result


def inventory(roots: dict[str, Path]) -> set[tuple[str, str]]:
    result = {
        ("program", path.relative_to(roots["program"]).as_posix())
        for path in program_files(roots["program"])
    }
    config = roots["config"]
    paths = [
        config / name
        for name in (".env", "docker-compose.yml", "docker-compose.tls.yml")
        if (config / name).exists() or (config / name).is_symlink()
    ]
    for name in ("infra", ".secrets"):
        paths.extend(tree_files(config / name, config))
    result.update(
        ("config", path.relative_to(config).as_posix())
        for path in paths
        if path.relative_to(config).as_posix() != ".secrets/backup-signing.key"
    )
    data = roots["data"]
    paths = [
        data / name for name in STATIC_FILES if (data / name).exists() or (data / name).is_symlink()
    ]
    for name in STATIC_TREES:
        paths.extend(tree_files(data / name, data))
    result.update(("data", path.relative_to(data).as_posix()) for path in paths)
    if len(result) > MAX_FILES:
        raise ValueError("Inventory limit exceeded")
    return result


def digest(path: Path, root: Path, deadline: float) -> tuple[int, str]:
    safe_file(path, root)
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise ValueError("File size limit exceeded")
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError("Integrity scan deadline exceeded")
            value.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError("File changed during the integrity scan")
    return before.st_size, value.hexdigest()


def compare_snapshot(
    backups: Path, name: str, key: bytes, roots: dict[str, Path]
) -> dict[str, Any]:
    started = datetime.now(UTC)
    manifest = read_manifest(backups, name, key)
    if manifest["schema"] != "dispatcher-backup-2":
        raise ValueError("A full snapshot including the program is required")
    expected = {
        (row["scope"], row["path"]): row
        for row in manifest["files"]
        if included(row["scope"], row["path"])
    }
    if len(expected) > MAX_FILES or sum(row["size"] for row in expected.values()) > MAX_BYTES:
        raise ValueError("Integrity scope exceeds limits")
    issues: list[dict[str, str]] = []
    total = 0
    issue_bytes = 0

    def record(scope: str, name: str, kind: str) -> None:
        nonlocal total, issue_bytes
        total += 1
        issue = {"scope": scope, "path": name, "kind": kind}
        size = len(json.dumps(issue).encode())
        if len(issues) < MAX_ISSUES and issue_bytes + size <= 128 * 1024:
            issues.append(issue)
            issue_bytes += size

    deadline = time.monotonic() + 120
    checked = 0
    byte_count = 0
    for (scope, name), row in sorted(expected.items()):
        try:
            size, actual = digest(roots[scope] / checked_relative(name), roots[scope], deadline)
            checked += 1
            byte_count += size
            if size != row["size"] or actual != row["sha256"]:
                record(scope, name, "CHANGED")
        except FileNotFoundError:
            record(scope, name, "MISSING")
        except ValueError:
            record(scope, name, "INVALID")
    try:
        actual_paths = inventory(roots)
        for scope, name in sorted(actual_paths - expected.keys()):
            record(scope, name, "EXTRA")
    except ValueError:
        record("program", "", "INVALID_INVENTORY")
    return {
        "status": "FAIL" if total else "OK",
        "snapshot": name,
        "started_at": started.isoformat(),
        "checked_at": datetime.now(UTC).isoformat(),
        "files": checked,
        "expected_files": len(expected),
        "bytes": byte_count,
        "issues": issues,
        "issues_total": total,
        "truncated": total > len(issues),
        "scopes": ["program", "config", "static_resources"],
    }


def select_baseline(
    backups: Path, key: bytes, request: dict[str, Any], roots: dict[str, Path]
) -> dict[str, Any]:
    value = compare_snapshot(backups, request["snapshot"], key, roots)
    if value["status"] != "OK":
        raise ValueError("Current files do not match the selected snapshot")
    previous = baseline(backups, key)
    if previous and previous["id"] == request["id"]:
        if any(previous[field] != request[field] for field in ("snapshot", "actor_id", "reason")):
            raise ValueError("Baseline request identity conflict")
        return {**value, "baseline": previous}
    selected = {field: request[field] for field in ("snapshot", "id", "actor_id", "reason")}
    selected["selected_at"] = datetime.now(UTC).isoformat()
    write_signed(backups / BASELINE, selected, key, PURPOSE)
    return {**value, "baseline": selected}


def check(backups: Path, key: bytes, roots: dict[str, Path]) -> dict[str, Any]:
    try:
        selected = baseline(backups, key)
        if selected is None:
            return {
                "status": "EMPTY",
                "checked_at": datetime.now(UTC).isoformat(),
                "baseline": None,
            }
        return {**compare_snapshot(backups, selected["snapshot"], key, roots), "baseline": selected}
    except (ValueError, KeyError, TypeError):
        return {
            "status": "FAIL",
            "code": "INVALID_BASELINE",
            "checked_at": datetime.now(UTC).isoformat(),
        }
    except (OSError, TimeoutError):
        return {
            "status": "UNAVAILABLE",
            "code": "SCAN_UNAVAILABLE",
            "checked_at": datetime.now(UTC).isoformat(),
        }
