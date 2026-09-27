"""Check local configuration secrets against deliverable files without displaying values."""

from pathlib import Path
import os

root = Path(__file__).resolve().parents[1]
values = dict(
    line.split("=", 1)
    for line in (root / ".env").read_text().splitlines()
    if "=" in line and not line.lstrip().startswith("#")
)
secrets = [
    values[name].encode()
    for name in ("JWT_SECRET", "POSTGRES_PASSWORD", "APP_DB_PASSWORD", "REDIS_PASSWORD")
]
assert all(len(secret) >= 24 for secret in secrets)
excluded = {
    ".env",
    ".git",
    ".tools",
    ".venv",
    ".secrets",
    ".backups",
    "node_modules",
    "artifacts",
    "backups",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "backend.profile",
}
checked = 0
violations = []
overlap = max(map(len, secrets)) - 1
for directory, folders, files in os.walk(root, followlinks=False):
    folders[:] = [name for name in folders if name not in excluded]
    for name in files:
        if name in excluded:
            continue
        path = Path(directory) / name
        if path.is_symlink():
            continue
        checked += 1
        with path.open("rb") as stream:
            previous = b""
            while chunk := stream.read(4 * 1024 * 1024):
                content = previous + chunk
                if any(secret in content for secret in secrets):
                    violations.append(str(path.relative_to(root)))
                    break
                previous = content[-overlap:]
assert not violations, f"Configuration secret found in: {violations}"
print(f"Checked {checked} deliverable files: no configuration secret values found.")
