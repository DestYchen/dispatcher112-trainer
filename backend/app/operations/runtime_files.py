"""One-shot local ownership and credential preparation; runtime services stay unprivileged."""

import os
import stat
import tempfile
from pathlib import Path

from app.operations.update_catalog import publisher

UID = 10001
GID = 10001
SERVICES = (
    "backend",
    "worker",
    "sip_worker",
    "telephony",
    "frontend",
    "gateway",
    "languagetool",
    "ollama",
)


def checked(path: Path, root: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError("Runtime path must stay within its mounted root")
    current = path
    while True:
        if current.is_symlink():
            raise ValueError("Runtime paths must not contain symbolic links")
        if current == root:
            break
        current = current.parent


def directory(path: Path, root: Path, *, mode: int = 0o700) -> None:
    checked(path, root)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not path.is_dir():
        raise ValueError("Expected a runtime directory")
    os.chown(path, UID, GID)
    path.chmod(mode)


def copy_secret(source: Path, destination: Path, source_root: Path, target_root: Path) -> None:
    checked(source, source_root)
    checked(destination, target_root)
    if not source.is_file() or source.stat().st_size > 32768:
        raise ValueError("Missing or invalid runtime credential")
    directory(destination.parent, target_root)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(source.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, UID, GID)
        os.chmod(temporary, 0o600)
        Path(temporary).replace(destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def writable_tree(path: Path, root: Path) -> None:
    checked(path, root)
    if path.exists() and not path.is_dir():
        raise ValueError("Expected a writable data directory")
    children = list(path.rglob("*")) if path.exists() else []
    for child in children:
        checked(child, root)
        if not (child.is_file() or child.is_dir()):
            raise ValueError("Unsupported file in a writable data directory")
    directory(path, root, mode=0o2770)
    for child in children:
        os.chown(child, UID, GID)
        child.chmod(0o2770 if child.is_dir() else 0o660)


def prepare(secrets: Path, data: Path, prepared: Path) -> None:
    for service in SERVICES:
        target = prepared / service
        directory(target, prepared)
        source = secrets / "pki" / service
        checked(source, secrets)
        if source.exists():
            for name in ("ca.crt", "tls.crt", "tls.key"):
                copy_secret(source / name, target / "tls" / name, secrets, target)
    for name in ("control", "backup-control"):
        copy_secret(
            secrets / name / "token",
            prepared / "backend" / name / "token",
            secrets,
            prepared / "backend",
        )
    public_source = secrets / "software-publisher.pub"
    public_target = prepared / "backend/software-publisher.pub"
    checked(public_source, secrets)
    checked(public_target, prepared)
    if publisher(public_source) is not None:
        copy_secret(public_source, public_target, secrets, prepared / "backend")
    else:
        public_target.unlink(missing_ok=True)
    for name in ("materials", "telephony", "backup-status", "backup-operations"):
        writable_tree(data / name, data)
    for name in ("speech", "recordings"):
        directory(data / "telephony" / name, data, mode=0o2770)
    for name in ("requests", "results"):
        directory(data / "backup-operations" / name, data, mode=0o2770)
    feedback = data / "feedback.jsonl"
    checked(feedback, data)
    if feedback.exists() and not stat.S_ISREG(feedback.stat().st_mode):
        raise ValueError("Expected a regular feedback file")
    if not feedback.exists():
        feedback.touch()
    os.chown(feedback, UID, GID)
    feedback.chmod(0o660)


def main() -> None:
    os.umask(0o077)
    if os.geteuid() != 0:
        raise SystemExit("Run the one-shot runtime preparation service as installation root")
    prepare(Path("/secrets"), Path("/data"), Path("/prepared"))
    print("Runtime files prepared; source credentials unchanged, no secrets printed.")


if __name__ == "__main__":
    main()
