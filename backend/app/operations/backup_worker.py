"""Backup-only executor; has no Docker socket, shell API, or arbitrary commands."""

import argparse
import json
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from app.operations import file_integrity
from app.operations.backup_protocol import (
    DEFAULT_SCHEDULE,
    read_job,
    read_signed,
    validate_request,
    validate_schedule,
    write_signed,
)
from app.operations.snapshot import read_manifest

ROOT = Path("/queue")
STATUS = Path("/status")
BACKUPS = Path("/backups")
TOKEN = Path("/config/.secrets/backup-control/token")
SIGNING_KEY = Path("/config/.secrets/backup-signing.key")
INTEGRITY_ROOTS = {"program": Path("/program"), "config": Path("/config"), "data": Path("/source")}


def check_files(key: bytes) -> dict[str, Any]:
    write_signed(
        STATUS / "file-integrity.json",
        {"status": "RUNNING", "started_at": datetime.now(UTC).isoformat()},
        key,
        "file-integrity",
    )
    result = file_integrity.check(BACKUPS, SIGNING_KEY.read_bytes().strip(), INTEGRITY_ROOTS)
    write_signed(STATUS / "file-integrity.json", result, key, "file-integrity")
    return result


def schedule(key: bytes) -> dict[str, Any]:
    path = ROOT / "schedule.json"
    return (
        validate_schedule(read_signed(path, key, "schedule"))
        if path.exists()
        else dict(DEFAULT_SCHEDULE)
    )


def publish_catalog(key: bytes) -> None:
    signing = SIGNING_KEY.read_bytes().strip()
    items = []
    for path in sorted((BACKUPS / "snapshots").glob("backup-*"), reverse=True):
        if path.name.endswith(".partial"):
            continue
        try:
            value = read_manifest(BACKUPS, path.name, signing)
            item = {
                "name": value["name"],
                "created_at": value["created_at"],
                "files": len(value["files"]),
                "bytes": sum(row["size"] for row in value["files"]),
                "program_files": sum(row["scope"] == "program" for row in value["files"]),
                "schema": value["schema"],
                "valid_manifest": True,
            }
        except (OSError, ValueError, KeyError, TypeError):
            item = {
                "name": path.name,
                "created_at": None,
                "files": None,
                "bytes": None,
                "program_files": None,
                "schema": None,
                "valid_manifest": False,
            }
        saved_verification = BACKUPS / "verifications" / f"{path.name}.json"
        if saved_verification.is_file():
            try:
                verification = read_signed(saved_verification, key, "verification")
                if verification.get("snapshot") != path.name:
                    raise ValueError("Verification belongs to a different snapshot")
                item["verification"] = verification
            except (OSError, ValueError, KeyError, TypeError):
                item["verification"] = None
        else:
            item["verification"] = None
        items.append(item)
    write_signed(
        STATUS / "catalog.json",
        {
            "items": items,
            "schedule": schedule(key),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        key,
        "catalog",
    )


def run_backup(*arguments: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        ["python3", "/scripts/full_backup.py", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("Backup command failed; existing source data was not replaced")
    if not completed.stdout.strip():
        return None
    value: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return value


def execute(request: dict[str, Any], key: bytes) -> None:
    validate_request(request)
    identity = UUID(request["id"])
    result: dict[str, Any] = {
        "id": str(identity),
        "request": request,
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
    }
    path = ROOT / "results" / f"{identity}.json"
    write_signed(path, result, key, "result")
    try:
        if request["kind"] == "backup_configure":
            value = validate_schedule(request["schedule"])
            write_signed(ROOT / "schedule.json", value, key, "schedule")
            outcome: dict[str, Any] | None = {"schedule": value}
        elif request["kind"] == "backup_verify":
            outcome = run_backup("--verify", request["snapshot"])
        elif request["kind"] == "backup_integrity_check":
            outcome = check_files(key)
        elif request["kind"] == "backup_integrity_baseline":
            verification = read_signed(
                BACKUPS / "verifications" / f"{request['snapshot']}.json", key, "verification"
            )
            if verification.get("snapshot") != request["snapshot"] or not verification.get(
                "verified_at"
            ):
                raise ValueError("Verify restoration before approving a baseline")
            outcome = file_integrity.select_baseline(
                BACKUPS, SIGNING_KEY.read_bytes().strip(), request, INTEGRITY_ROOTS
            )
            write_signed(STATUS / "file-integrity.json", outcome, key, "file-integrity")
        else:
            outcome = run_backup()
        if outcome is None:
            raise RuntimeError("Backup command did not return a result")
        publish_catalog(key)
        result.update(status="SUCCEEDED", result=outcome)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        result.update(
            status="FAILED",
            error=(
                "Эталон не принят. Проверьте восстановление выбранной полной копии и "
                "совпадение текущих файлов с ней. Исходные файлы не заменялись."
                if request["kind"] == "backup_integrity_baseline"
                else "Операция резервирования не завершена. Исходная база не заменялась; "
                "проверьте доступность хранилища и целостность копии."
            ),
        )
    result["finished_at"] = datetime.now(UTC).isoformat()
    write_signed(path, result, key, "result")


def recover(key: bytes) -> None:
    for path in (ROOT / "requests").glob("*.json"):
        try:
            identity = UUID(path.stem)
            result = read_job(ROOT, identity, key)
            if result["status"] == "RUNNING":
                result.update(
                    status="FAILED",
                    finished_at=datetime.now(UTC).isoformat(),
                    error="Исполнитель был перезапущен. Проверьте список копий и повторите "
                    "проверку восстановления отдельной операцией.",
                )
                write_signed(ROOT / "results" / path.name, result, key, "result")
        except (OSError, ValueError, KeyError, TypeError):
            print("Rejected an invalid backup operation file", flush=True)


def process_one(key: bytes) -> bool:
    for path in sorted((ROOT / "requests").glob("*.json")):
        try:
            value = read_job(ROOT, UUID(path.stem), key)
            if value["status"] == "QUEUED":
                execute(value["request"], key)
                return True
        except (OSError, ValueError, KeyError, TypeError):
            print("Rejected an invalid backup operation file", flush=True)
    return False


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true")
    arguments = parser.parse_args()
    key = TOKEN.read_bytes().strip()
    if arguments.health:
        health = read_signed(STATUS / "worker.json", key, "heartbeat")
        if datetime.fromisoformat(health["at"]) < datetime.now(UTC) - timedelta(seconds=15):
            raise SystemExit("Backup executor heartbeat expired")
        return
    BACKUPS.mkdir(parents=True, exist_ok=True)
    with (BACKUPS / "worker.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopping = threading.Event()

        def stop(signum: int, frame: Any) -> None:
            stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        def pulse() -> None:
            while not stopping.is_set():
                write_signed(
                    STATUS / "worker.json", {"at": datetime.now(UTC).isoformat()}, key, "heartbeat"
                )
                stopping.wait(2)

        heartbeat = threading.Thread(target=pulse, daemon=True)
        heartbeat.start()
        recover(key)
        publish_catalog(key)
        next_scheduled = 0.0
        next_integrity = 0.0
        while not stopping.is_set():
            process_one(key)
            if time.monotonic() >= next_integrity and not stopping.is_set():
                try:
                    check_files(key)
                except (OSError, ValueError, KeyError, TypeError):
                    print("File integrity result unavailable", flush=True)
                next_integrity = time.monotonic() + 300
            if time.monotonic() >= next_scheduled and not stopping.is_set():
                try:
                    run_backup("--scheduled")
                    publish_catalog(key)
                except (OSError, ValueError, RuntimeError):
                    print("Scheduled backup failed; retry in 15 minutes", flush=True)
                    next_scheduled = time.monotonic() + 900
                else:
                    next_scheduled = time.monotonic() + 30
            stopping.wait(1)
        heartbeat.join(timeout=3)


if __name__ == "__main__":
    main()
