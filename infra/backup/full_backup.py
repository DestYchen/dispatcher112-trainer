"""Create or independently verify a complete local recovery snapshot."""

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.operations.snapshot import create_snapshot, export_snapshot
from app.operations.backup_protocol import (
    DEFAULT_SCHEDULE,
    read_signed,
    scheduled_due,
    validate_schedule,
    write_signed,
)

BACKUPS = Path("/backups")


def run(*arguments: str) -> str:
    result = subprocess.run(arguments, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"{arguments[0]} failed (exit {result.returncode}); source data unchanged"
        )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--verify", metavar="SNAPSHOT")
    args = parser.parse_args()
    os.umask(0o077)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    key = Path("/config/.secrets/backup-signing.key").read_bytes().strip()
    control_key = Path("/config/.secrets/backup-control/token").read_bytes().strip()
    schedule_path = Path("/queue/schedule.json")
    schedule = dict(DEFAULT_SCHEDULE)
    if schedule_path.exists():
        schedule = validate_schedule(
            read_signed(schedule_path, control_key, "schedule")
        )
    with (BACKUPS / ".lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.scheduled:
            now = datetime.now().astimezone()
            marker = BACKUPS / "manifest.json"
            last = (
                json.loads(marker.read_text())["last_backup_at"]
                if marker.exists()
                else None
            )
            if not scheduled_due(schedule, now, last):
                return
        with tempfile.TemporaryDirectory(prefix="dispatcher-backup-") as temporary:
            root = Path(temporary)
            if args.verify:
                manifest = export_snapshot(BACKUPS, args.verify, key, root / "restored")
                database = "dispatcher_verify_" + uuid4().hex[:16]
                run("createdb", "--template=template0", database)
                try:
                    run(
                        "pg_restore",
                        "--exit-on-error",
                        "--no-owner",
                        "--dbname",
                        database,
                        str(root / "restored/database/database.dump"),
                    )
                    counts = run(
                        "psql",
                        "--dbname",
                        database,
                        "--tuples-only",
                        "--no-align",
                        "--command",
                        "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM incident_types), (SELECT count(*) FROM audit_log), (SELECT count(*) FROM pg_trigger WHERE tgrelid='audit_log'::regclass AND NOT tgisinternal)",
                    )
                    users, types, audit, triggers = map(int, counts.split("|"))
                    if triggers < 1:
                        raise RuntimeError("Restored audit protection is missing")
                    database_role = run(
                        "psql",
                        "--dbname",
                        database,
                        "--tuples-only",
                        "--no-align",
                        "--command",
                        "SELECT current_user",
                    )
                    result = {
                        "snapshot": args.verify,
                        "verified_at": datetime.now().astimezone().isoformat(),
                        "restored_users": users,
                        "restored_types": types,
                        "restored_audit": audit,
                        "audit_triggers": triggers,
                        "files": len(manifest["files"]),
                        "program_files": sum(item["scope"] == "program" for item in manifest["files"]),
                        "schema": manifest["schema"],
                        "source_unchanged": True,
                        "database_role": database_role,
                    }
                finally:
                    run("dropdb", database)
                write_signed(
                    BACKUPS / "verifications" / f"{args.verify}.json",
                    result,
                    control_key,
                    "verification",
                )
                destination = BACKUPS / "verification.json"
                pending = destination.with_suffix(".partial")
                with pending.open("w", encoding="utf-8") as stream:
                    json.dump(result, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                pending.replace(destination)
                print(json.dumps(result))
            else:
                dump = root / "database.dump"
                run("pg_dump", "--format=custom", "--no-owner", "--file", str(dump))
                run("pg_restore", "--list", str(dump))
                result = create_snapshot(
                    BACKUPS,
                    Path("/source"),
                    Path("/config"),
                    dump,
                    key,
                    retention=schedule["retention"],
                    program_root=Path("/program"),
                )
                status = Path("/status")
                status.mkdir(parents=True, exist_ok=True)
                marker = status / "manifest.json.partial"
                marker.write_text(
                    json.dumps(
                        {
                            "last_backup_at": result["created_at"],
                            "snapshot": result["name"],
                        }
                    ),
                    encoding="utf-8",
                )
                marker.chmod(0o644)
                marker.replace(status / "manifest.json")
                print(json.dumps(result))


if __name__ == "__main__":
    main()
