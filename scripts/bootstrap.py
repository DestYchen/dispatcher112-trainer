"""Create local development secrets once, without printing their values."""

from pathlib import Path
from secrets import token_urlsafe


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    data = root / "data"
    for relative in (
        "materials",
        "telephony/speech",
        "telephony/recordings",
        "backup-status",
        "backup-operations/requests",
        "backup-operations/results",
    ):
        directory = data / relative
        if directory.is_symlink() or any(
            parent.is_symlink()
            for parent in directory.parents
            if parent.is_relative_to(root)
        ):
            raise ValueError("Runtime data directories must not be symbolic links")
        directory.mkdir(parents=True, exist_ok=True)
    feedback = data / "feedback.jsonl"
    if feedback.is_symlink():
        raise ValueError("Feedback must not be a symbolic link")
    if not feedback.exists():
        feedback.touch()
    control = root / ".secrets" / "control"
    control.mkdir(parents=True, exist_ok=True)
    if not (control / "token").exists():
        previous = root / "data/control/token"
        value = (
            previous.read_text(encoding="utf-8")
            if previous.exists()
            else token_urlsafe(48)
        )
        (control / "token").write_text(value, encoding="utf-8")
    backup_key = root / ".secrets/backup-signing.key"
    if not backup_key.exists():
        backup_key.write_text(token_urlsafe(48), encoding="utf-8")
    for directory in (root / ".secrets", control):
        directory.chmod(0o700)
    (control / "token").chmod(0o600)
    backup_key.chmod(0o600)
    backup_control = root / ".secrets/backup-control"
    backup_control.mkdir(parents=True, exist_ok=True)
    backup_control.chmod(0o700)
    if not (backup_control / "token").exists():
        (backup_control / "token").write_text(token_urlsafe(48), encoding="utf-8")
    (backup_control / "token").chmod(0o600)
    target = root / ".env"
    if target.exists():
        content = target.read_text(encoding="utf-8")
        for name in ("REDIS_PASSWORD", "APP_DB_PASSWORD", "BACKUP_DB_PASSWORD"):
            if not any(line.startswith(f"{name}=") for line in content.splitlines()):
                with target.open("a", encoding="utf-8", newline="\n") as output:
                    output.write(f"\n{name}={token_urlsafe(32)}\n")
        print("Using existing .env")
        return
    values = (root / ".env.example").read_text(encoding="utf-8")
    values = values.replace(
        "POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={token_urlsafe(32)}\n"
    )
    values = values.replace("JWT_SECRET=\n", f"JWT_SECRET={token_urlsafe(48)}\n")
    values = values.replace(
        "REDIS_PASSWORD=\n", f"REDIS_PASSWORD={token_urlsafe(32)}\n"
    )
    values = values.replace(
        "APP_DB_PASSWORD=\n", f"APP_DB_PASSWORD={token_urlsafe(32)}\n"
    )
    values = values.replace(
        "BACKUP_DB_PASSWORD=\n", f"BACKUP_DB_PASSWORD={token_urlsafe(32)}\n"
    )
    with target.open("x", encoding="utf-8", newline="\n") as output:
        output.write(values)
    print("Created .env with random local secrets")


if __name__ == "__main__":
    main()
