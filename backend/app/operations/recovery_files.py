"""One-shot offline extraction into a new recovery volume; never touches the source."""

import argparse
import json
import os
import re
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from app.operations.backup_protocol import DEFAULT_SCHEDULE, read_signed, write_signed
from app.operations.snapshot import export_snapshot, read_manifest


def stage(backups: Path, name: str, key: bytes, target: Path) -> dict[str, Any]:
    manifest = read_manifest(backups, name, key)
    if manifest["schema"] != "dispatcher-backup-2":
        raise ValueError("Recovery needs a snapshot containing the application source")
    if target.is_symlink() or (target.exists() and list(target.iterdir())):
        raise ValueError("Recovery requires a new empty volume")
    # Program and configuration share a workspace after extraction. Validate before writing.
    paths = [item["path"] for item in manifest["files"] if item["scope"] in {"program", "config"}]
    if len(set(paths)) != len(paths) or any(Path(path).parts[0] == "data" for path in paths):
        raise ValueError("Conflicting recovery workspace paths")
    extracted = target / "snapshot"
    export_snapshot(backups, name, key, extracted)
    workspace = target / "workspace"
    (extracted / "program").rename(workspace)
    for child in (extracted / "config").iterdir():
        destination = workspace / child.name
        if destination.exists():
            raise ValueError("Conflicting recovery configuration directory")
        child.rename(destination)
    if (extracted / "data").exists():
        (extracted / "data").rename(workspace / "data")
    else:
        (workspace / "data").mkdir()
    for path in [workspace, *workspace.rglob("*")]:
        private = ".secrets" in path.relative_to(workspace).parts or path.name == ".env"
        path.chmod(
            (0o700 if private else 0o755) if path.is_dir() else (0o600 if private else 0o644)
        )
    secret = workspace / ".secrets/backup-signing.key"
    secret.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret.write_bytes(key)
    secret.chmod(0o600)
    # Redis contains revocations, not an allow-list. A fresh Redis needs a fresh signing key.
    environment = workspace / ".env"
    content = environment.read_text(encoding="utf-8")
    session_key = "JWT_SECRET=" + token_urlsafe(48)
    content, replaced = re.subn(r"(?m)^\s*(?:export\s+)?JWT_SECRET\s*=.*$", session_key, content)
    environment.write_text(
        content if replaced else content.rstrip() + "\n" + session_key + "\n", encoding="utf-8"
    )
    token = (workspace / ".secrets/backup-control/token").read_bytes().strip()
    queue = workspace / "data/backup-operations"
    schedule = (
        read_signed(queue / "schedule.json", token, "schedule")
        if (queue / "schedule.json").exists()
        else dict(DEFAULT_SCHEDULE)
    )
    history = workspace / "data/recovery-history" / name
    history.mkdir(parents=True, mode=0o700)
    for relative in ("backup-operations", "backup-status", "operations"):
        previous = workspace / "data" / relative
        if previous.exists():
            previous.rename(history / relative)
        previous.mkdir(mode=0o700)
    write_signed(queue / "schedule.json", {**schedule, "enabled": False}, token, "schedule")
    for name_in_queue in ("requests", "results"):
        (queue / name_in_queue).mkdir()
    # Active channels do not survive the old PBX. The recovered SIP worker rebuilds accounts.
    accounts = workspace / "data/telephony/accounts.conf"
    accounts.parent.mkdir(parents=True, exist_ok=True)
    if accounts.exists():
        accounts.rename(history / "sip-accounts.conf")
    accounts.touch(mode=0o660)
    for relative in ("materials", "telephony/speech", "telephony/recordings", "llm/models"):
        (workspace / "data" / relative).mkdir(parents=True, exist_ok=True)
    (workspace / "data/feedback.jsonl").touch(exist_ok=True)
    result = {
        "snapshot": name,
        "prepared_at": datetime.now(UTC).isoformat(),
        "files": len(manifest["files"]),
        "program_files": sum(item["scope"] == "program" for item in manifest["files"]),
        "original_schedule": schedule,
        "session_key_rotated": True,
    }
    (target / "extraction.json").write_text(json.dumps(result), encoding="utf-8")
    return result


def archive_workspace(workspace: Path) -> None:
    """Models stay in the recovery volume; stream only the small host workspace."""
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace)
            if relative.parts[:2] == ("data", "llm"):
                continue
            if path.is_symlink():
                raise ValueError("Recovery workspace cannot contain symbolic links")
            archive.add(path, arcname=relative.as_posix(), recursive=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot")
    parser.add_argument("--archive", action="store_true")
    arguments = parser.parse_args()
    os.umask(0o077)
    if arguments.archive:
        archive_workspace(Path("/recovery/workspace"))
    elif arguments.snapshot:
        key = Path("/signing-key").read_bytes().strip()
        print(json.dumps(stage(Path("/backups"), arguments.snapshot, key, Path("/recovery"))))
    else:
        parser.error("Supply --snapshot or --archive")


if __name__ == "__main__":
    main()
