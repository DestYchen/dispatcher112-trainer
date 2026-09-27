"""Manage a read-only local monitor without a listening port or Docker socket mount."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def file_path(root, name):
    path = root / ".runtime" / name
    if path.resolve() != path:
        raise ValueError("Monitor files cannot use symbolic links")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read(root, name):
    path = file_path(root, name)
    if not path.exists():
        return {}
    if path.stat().st_size > 16384:
        raise ValueError("Invalid monitor state")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Invalid monitor state")
    return result


def write(root, name, value):
    path = file_path(root, name)
    temporary = file_path(root, f"{name}.{os.getpid()}.partial")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def active(root, token):
    value = read(root, "monitor.json")
    return value.get("enabled") is True and value.get("token") == token


def heartbeat(root, token, result):
    if active(root, token):
        write(
            root,
            "monitor-status.json",
            {
                "token": token,
                "at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                **result,
            },
        )


def status(root):
    control = read(root, "monitor.json")
    sample = read(root, "monitor-status.json")
    state = "stopped"
    if control.get("enabled"):
        state = "unavailable"
        if (
            datetime.now(UTC) - datetime.fromisoformat(control["started_at"])
        ).total_seconds() < 10:
            state = "starting"
        if sample.get("token") == control.get("token"):
            age = (
                datetime.now(UTC) - datetime.fromisoformat(sample["at"])
            ).total_seconds()
            if 0 <= age <= 60:
                state = (
                    "running"
                    if sample.get("status") != "unavailable"
                    else "unavailable"
                )
    return {
        "status": state,
        "sample": {key: value for key, value in sample.items() if key != "token"},
    }


def start(root):
    current = status(root)
    if current["status"] in {"starting", "running"}:
        return current
    # Replacing the token revokes any old monitor without killing a possibly reused PID.
    token = uuid4().hex
    write(
        root,
        "monitor.json",
        {"enabled": True, "token": token, "started_at": datetime.now(UTC).isoformat()},
    )
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "run",
                "--installation",
                str(root),
                "--token",
                token,
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        stop(root)
        raise
    return {"status": "starting"}


def stop(root):
    write(root, "monitor.json", {"enabled": False})
    return {"status": "stopped"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "run"])
    parser.add_argument("--installation", type=Path, default=ROOT)
    parser.add_argument("--token")
    args = parser.parse_args()
    root = args.installation.resolve()
    if args.action == "run":
        if not args.token or not active(root, args.token):
            raise ValueError("Monitor start was superseded")
        from technical_operations import watch

        watch(root, 10, token=args.token)
        return
    result = {"start": start, "stop": stop, "status": status}[args.action](root)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError):
        raise SystemExit(
            "Local monitor is unavailable; inspect .runtime/monitor-status.json"
        ) from None
