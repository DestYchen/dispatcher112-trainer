"""Content-addressed local backups; all paths and digests are checked before restore."""

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from app.operations.backup_protocol import read_signed
from app.operations.program_files import REQUIRED_PROGRAM_FILES, program_files

SNAPSHOT_NAME = re.compile(r"^backup-\d{8}T\d{6}Z-[a-f0-9]{12}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.as_posix() != value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
    ):
        raise ValueError("Недопустимый путь в резервной копии.")
    return path


def store_file(source: Path, objects: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Резервная копия не должна содержать символические ссылки.")
    before = source.stat()
    digest = file_digest(source)
    target = objects / digest
    if not target.exists():
        descriptor, name = tempfile.mkstemp(prefix="object-", suffix=".partial", dir=objects)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if file_digest(Path(name)) != digest:
                raise ValueError("Файл изменился при копировании. Повторите резервирование.")
            Path(name).replace(target)
        finally:
            Path(name).unlink(missing_ok=True)
    elif file_digest(target) != digest:
        raise ValueError("Повреждён объект хранилища резервных копий.")
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Файл изменился при копировании. Повторите резервирование.")
    return {"sha256": digest, "size": before.st_size}


def read_manifest(backups: Path, name: str, key: bytes) -> dict[str, Any]:
    if not SNAPSHOT_NAME.fullmatch(name):
        raise ValueError("Недопустимое имя резервной копии.")
    path = backups / "snapshots" / name / "manifest.json"
    if path.is_symlink():
        raise ValueError("Недопустимый манифест резервной копии.")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    manifest: dict[str, Any] = envelope["manifest"]
    expected = hmac.new(key, canonical(manifest), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope["hmac_sha256"]):
        raise ValueError("Подпись резервной копии не совпадает.")
    version = manifest["schema"]
    if version not in {"dispatcher-backup-1", "dispatcher-backup-2"} or manifest["name"] != name:
        raise ValueError("Неподдерживаемый формат резервной копии.")
    scopes = {"data", "config", "database"}
    if version == "dispatcher-backup-2":
        scopes.add("program")
    seen = set()
    for item in manifest["files"]:
        checked_relative(item["path"])
        identity = (item["scope"], item["path"])
        if item["scope"] not in scopes or identity in seen:
            raise ValueError("Недопустимое содержимое резервной копии.")
        seen.add(identity)
        if (
            not DIGEST.fullmatch(item["sha256"])
            or not isinstance(item["size"], int)
            or item["size"] < 0
        ):
            raise ValueError("Недопустимая контрольная сумма резервной копии.")
    if sum(item["scope"] == "database" for item in manifest["files"]) != 1:
        raise ValueError("В копии должен находиться один снимок базы данных.")
    if ("database", "database.dump") not in seen:
        raise ValueError("В копии отсутствует снимок database.dump.")
    if version == "dispatcher-backup-2" and not all(
        ("program", path) in seen for path in REQUIRED_PROGRAM_FILES
    ):
        raise ValueError("В копии отсутствуют обязательные файлы программы.")
    return manifest


def verify_snapshot(backups: Path, name: str, key: bytes) -> dict[str, Any]:
    manifest = read_manifest(backups, name, key)
    for item in manifest["files"]:
        path = backups / "objects" / item["sha256"]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["size"]
            or file_digest(path) != item["sha256"]
        ):
            raise ValueError("Файл резервной копии отсутствует или повреждён.")
    return manifest


def create_snapshot(
    backups: Path,
    data_root: Path,
    config_root: Path,
    database_dump: Path,
    key: bytes,
    retention: int = 14,
    *,
    program_root: Path | None = None,
) -> dict[str, Any]:
    if not 14 <= retention <= 90:
        raise ValueError("Нужно хранить от 14 до 90 копий.")
    sources = program_files(program_root) if program_root is not None else []
    objects = backups / "objects"
    snapshots = backups / "snapshots"
    objects.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    files: list[dict[str, Any]] = []
    for source in sorted(data_root.rglob("*")):
        relative = source.relative_to(data_root)
        if relative.parts[0] == "backups" or source.name.endswith(".partial"):
            continue
        if source.is_symlink():
            raise ValueError("В данных обнаружена символическая ссылка.")
        if source.is_file():
            files.append(
                {"scope": "data", "path": relative.as_posix(), **store_file(source, objects)}
            )
    config_paths = [
        config_root / ".env",
        config_root / "docker-compose.yml",
        config_root / "docker-compose.tls.yml",
    ]
    if (config_root / "infra").exists():
        config_paths.extend(sorted((config_root / "infra").rglob("*")))
    if (config_root / ".secrets").exists():
        config_paths.extend(sorted((config_root / ".secrets").rglob("*")))
    for source in config_paths:
        if source.relative_to(config_root).as_posix() == ".secrets/backup-signing.key":
            continue
        if source.is_symlink():
            raise ValueError("В конфигурации обнаружена символическая ссылка.")
        if source.is_file():
            files.append(
                {
                    "scope": "config",
                    "path": source.relative_to(config_root).as_posix(),
                    **store_file(source, objects),
                }
            )
    files.append(
        {"scope": "database", "path": "database.dump", **store_file(database_dump, objects)}
    )
    if program_root is not None:
        for source in sources:
            files.append(
                {
                    "scope": "program",
                    "path": source.relative_to(program_root).as_posix(),
                    **store_file(source, objects),
                }
            )
        if sources != program_files(program_root) or any(
            file_digest(program_root / item["path"]) != item["sha256"]
            for item in files
            if item["scope"] == "program"
        ):
            raise ValueError("Программа изменилась при копировании. Повторите резервирование.")
    now = datetime.now(UTC)
    name = f"backup-{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}"
    manifest = {
        "schema": "dispatcher-backup-2" if program_root is not None else "dispatcher-backup-1",
        "name": name,
        "created_at": now.isoformat(),
        "files": files,
    }
    envelope = {
        "manifest": manifest,
        "hmac_sha256": hmac.new(key, canonical(manifest), hashlib.sha256).hexdigest(),
    }
    temporary = snapshots / (name + ".partial")
    temporary.mkdir(mode=0o700)
    (temporary / "manifest.json").write_text(
        json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(snapshots / name)
    marker = backups / "manifest.json.partial"
    marker.write_text(
        json.dumps({"last_backup_at": now.isoformat(), "snapshot": name}), encoding="utf-8"
    )
    marker.replace(backups / "manifest.json")
    prune_snapshots(backups, key, retention)
    return {
        "name": name,
        "created_at": now.isoformat(),
        "files": len(files),
        "bytes": sum(item["size"] for item in files),
        "program_files": len(sources),
        "schema": manifest["schema"],
    }


def prune_snapshots(backups: Path, key: bytes, retention: int) -> None:
    if not 14 <= retention <= 90:
        raise ValueError("Нужно хранить от 14 до 90 копий.")
    directory = (backups / "snapshots").resolve()
    paths = sorted(path for path in directory.iterdir() if SNAPSHOT_NAME.fullmatch(path.name))
    # Validate every manifest before removing anything, including shared objects.
    values = {path.name: read_manifest(backups, path.name, key) for path in paths}
    paths.sort(key=lambda path: (values[path.name]["created_at"], path.name))
    old, keep = paths[:-retention], paths[-retention:]
    marker = backups / "integrity-baseline.json"
    if marker.exists() or marker.is_symlink():
        selected = read_signed(marker, key, "installation-integrity-baseline-1")
        name = selected.get("snapshot")
        if not isinstance(name, str) or not SNAPSHOT_NAME.fullmatch(name) or name not in values:
            raise ValueError("The pinned integrity snapshot is missing or invalid")
        pinned = [path for path in old if path.name == name]
        old = [path for path in old if path.name != name]
        keep.extend(pinned)
    for path in old:
        if path.is_symlink() or path.resolve().parent != directory:
            raise ValueError("Недопустимый путь при очистке резервных копий.")
        shutil.rmtree(path)
    retained = {item["sha256"] for path in keep for item in values[path.name]["files"]}
    for path in (backups / "objects").iterdir():
        if DIGEST.fullmatch(path.name) and path.name not in retained:
            path.unlink()


def export_snapshot(backups: Path, name: str, key: bytes, destination: Path) -> dict[str, Any]:
    manifest = verify_snapshot(backups, name, key)
    if destination.exists():
        raise ValueError("Для проверки восстановления нужен новый пустой каталог.")
    destination.mkdir(mode=0o700, parents=True)
    for item in manifest["files"]:
        target = destination / item["scope"] / checked_relative(item["path"])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(backups / "objects" / item["sha256"], target)
        target.chmod(0o600)
    return manifest
