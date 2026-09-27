"""Verify full files/configuration and restore PostgreSQL into an isolated database."""

import json
import subprocess
from pathlib import Path


def run(*arguments: str) -> None:
    subprocess.run(["docker", "compose", *arguments], check=True)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    run("run", "--rm", "backup", "/scripts/backup.sh")
    marker = json.loads((root / ".backups/manifest.json").read_text(encoding="utf-8"))
    name = marker["snapshot"]
    if (
        not isinstance(name, str)
        or not name.startswith("backup-")
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("Invalid backup identifier")
    run("run", "--rm", "backup", "python3", "/scripts/full_backup.py", "--verify", name)
    result = json.loads(
        (root / ".backups/verification.json").read_text(encoding="utf-8")
    )
    if (
        result["snapshot"] != name
        or not result["source_unchanged"]
        or result["audit_triggers"] < 1
    ):
        raise RuntimeError("The backup could not be verified")
    target = root / "artifacts/ui-review/full-backup-verification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
