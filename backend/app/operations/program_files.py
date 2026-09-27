"""Explicit source inventory needed to recover this application's installed release."""

import os
import stat
from pathlib import Path

PROGRAM_TREES = (
    "backend/app",
    "backend/alembic",
    "backend/tests",
    "frontend/src",
    "frontend/public",
    "scripts",
    "docs",
)
PROGRAM_FILES = (
    "backend/Dockerfile",
    "backend/.dockerignore",
    "backend/pyproject.toml",
    "backend/requirements.lock",
    "backend/alembic.ini",
    "frontend/Dockerfile",
    "frontend/.dockerignore",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "frontend/eslint.config.js",
    "frontend/index.html",
    "Makefile",
    ".env.example",
    ".dockerignore",
    "README.md",
    "README-SPEC.md",
    "SPEC.md",
    "API.md",
    "UI.md",
    "PLAN.md",
)
REQUIRED_PROGRAM_FILES = frozenset(
    (
        *PROGRAM_FILES,
        "backend/app/main.py",
        "backend/alembic/env.py",
        "frontend/src/main.tsx",
        "frontend/src/styles/tokens.css",
        "scripts/bootstrap.py",
        "scripts/prepare_tls.py",
    )
)
EXCLUDED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        ".venv",
        "node_modules",
        "dist",
    }
)


def checked_source(path: Path, root: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError("Program source must stay inside its mounted root")
    for current in (path, *path.parents):
        if current.is_symlink():
            raise ValueError("Program source must not contain symbolic links")
        if current == root:
            break


def program_files(root: Path) -> list[Path]:
    """Fail before publishing a backup when a required source mount is absent."""

    def failed(error: OSError) -> None:
        raise error

    files = [root / name for name in PROGRAM_FILES]
    for relative in PROGRAM_TREES:
        directory = root / relative
        checked_source(directory, root)
        if not directory.is_dir():
            raise ValueError(f"Required program directory is missing: {relative}")
        for current, directories, names in os.walk(directory, followlinks=False, onerror=failed):
            directories[:] = sorted(set(directories) - EXCLUDED_DIRECTORIES)
            for child in directories:
                checked_source(Path(current) / child, root)
            for name in sorted(names):
                if (
                    name.endswith((".pyc", ".pyo", ".tsbuildinfo", ".partial", ".log"))
                    or name == ".env"
                    or name.startswith(".env.")
                ):
                    continue
                files.append(Path(current) / name)
    for path in files:
        checked_source(path, root)
        if not path.exists() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(
                f"Required program file is missing or invalid: {path.relative_to(root)}"
            )
    present = {path.relative_to(root).as_posix() for path in files}
    if not REQUIRED_PROGRAM_FILES.issubset(present):
        raise ValueError("Required application entry points are missing")
    return sorted(files)
