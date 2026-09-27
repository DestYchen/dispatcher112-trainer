"""Signed, bounded offline software packages; extraction never modifies an installation."""

import base64
import hashlib
import json
import re
import stat
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.operations.program_files import (
    EXCLUDED_DIRECTORIES,
    PROGRAM_FILES,
    PROGRAM_TREES,
    REQUIRED_PROGRAM_FILES,
    checked_source,
    program_files,
)
from app.operations.recovery_compose import SERVICES
from app.operations.snapshot import canonical, checked_relative, file_digest

MAX_FILES = 10_000
MAX_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
FORMAT = "dispatcher-update-1"
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DIGEST = re.compile(r"[a-f0-9]{64}")
DEPLOYMENT_FILES = ("docker-compose.yml", "docker-compose.tls.yml")


def package_path(value: str) -> str:
    path = checked_relative(value)
    if any(
        part.endswith((".", " "))
        or any(ord(char) < 32 or char in '<>"|?*' for char in part)
        or part.upper().split(".")[0]
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        or part.casefold() in {".secrets", ".env", *EXCLUDED_DIRECTORIES}
        or part.casefold().startswith(".env.")
        and value != ".env.example"
        for part in path.parts
    ):
        raise ValueError("Недопустимое имя файла пакета обновления.")
    if not (
        value in {*PROGRAM_FILES, *DEPLOYMENT_FILES}
        or any(value.startswith(tree + "/") for tree in (*PROGRAM_TREES, "infra"))
    ):
        raise ValueError(
            "Пакет не может заменять данные, секреты или произвольные файлы установки."
        )
    if path.suffix.lower() in {".key", ".pem", ".pfx", ".p12", ".pyc", ".pyo"}:
        raise ValueError("Закрытые ключи и скомпилированные файлы не входят в пакет программы.")
    return value


def release_files(root: Path) -> list[Path]:
    paths = program_files(root)
    paths.extend(root / name for name in DEPLOYMENT_FILES)
    infra = root / "infra"
    if not infra.is_dir() or infra.is_symlink():
        raise ValueError("Отсутствуют инфраструктурные файлы программы.")
    for path in sorted(infra.rglob("*")):
        if any(part in EXCLUDED_DIRECTORIES for part in path.relative_to(infra).parts):
            continue
        if path.is_symlink():
            raise ValueError("Символические ссылки не допускаются в пакете.")
        if path.is_file():
            paths.append(path)
    for path in paths:
        checked_source(path, root)
        package_path(path.relative_to(root).as_posix())
        if path.is_symlink() or not path.is_file():
            raise ValueError("Файл программы отсутствует или является ссылкой.")
    return sorted(paths)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющееся поле в описании обновления.")
        result[key] = value
    return result


def validate_manifest(value: dict[str, Any]) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "version", "created_at", "description", "images", "files"}
        or value["schema"] != FORMAT
    ):
        raise ValueError("Неподдерживаемый формат пакета обновления.")
    if not isinstance(value["version"], str) or not VERSION.fullmatch(value["version"]):
        raise ValueError("Недопустимая версия обновления.")
    if (
        not isinstance(value["description"], str)
        or not 5 <= len(value["description"].strip()) <= 2000
    ):
        raise ValueError("Пакету требуется описание изменений.")
    if not isinstance(value["created_at"], str):
        raise ValueError("Время выпуска должно быть строкой ISO 8601.")
    created = datetime.fromisoformat(value["created_at"])
    if created.tzinfo is None:
        raise ValueError("Время выпуска должно содержать часовой пояс.")
    images = value["images"]
    if not isinstance(images, dict) or set(images) != SERVICES:
        raise ValueError("Пакет должен указать локальные образы всех сервисов.")
    if any(
        not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
        for image in images.values()
    ):
        raise ValueError("Образы пакета должны быть закреплены по SHA-256.")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Недопустимое число файлов пакета.")
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("Недопустимое описание файла пакета.")
        if not isinstance(item["path"], str):
            raise ValueError("Путь файла должен быть строкой.")
        path = package_path(item["path"])
        if path.casefold() in seen:
            raise ValueError("Повторяющиеся имена файлов пакета.")
        seen.add(path.casefold())
        if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_BYTES:
            raise ValueError("Недопустимый размер файла пакета.")
        if not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
            raise ValueError("Недопустимая контрольная сумма файла.")
        total += item["size"]
    if total > MAX_BYTES:
        raise ValueError("Пакет программы превышает допустимый размер.")
    if not {*REQUIRED_PROGRAM_FILES, *DEPLOYMENT_FILES}.issubset({item["path"] for item in files}):
        raise ValueError("Пакет не содержит обязательные файлы программы.")


def create_package(
    root: Path,
    destination: Path,
    private_key: bytes,
    version: str,
    description: str,
    images: dict[str, str],
) -> dict[str, Any]:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Файл пакета уже существует.")
    key = serialization.load_pem_private_key(private_key, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Требуется закрытый ключ Ed25519.")
    sources = release_files(root)
    files: list[dict[str, Any]] = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": file_digest(path),
        }
        for path in sources
    ]
    manifest = {
        "schema": FORMAT,
        "version": version,
        "created_at": datetime.now(UTC).isoformat(),
        "description": description,
        "images": images,
        "files": files,
    }
    validate_manifest(manifest)
    envelope = canonical(
        {
            "manifest": manifest,
            "signature": base64.b64encode(key.sign(canonical(manifest))).decode("ascii"),
        }
    )
    if len(envelope) > MAX_MANIFEST_BYTES:
        raise ValueError("Описание пакета слишком большое.")
    temporary = destination.with_name(destination.name + ".partial")
    with temporary.open("xb") as stream:
        try:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as package:
                package.writestr("release.json", envelope)
                for source, item in zip(sources, files, strict=True):
                    package.write(source, "program/" + item["path"])
            if sources != release_files(root) or any(
                path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]
                for path, item in zip(sources, files, strict=True)
            ):
                raise ValueError("Исходники изменились во время сборки пакета.")
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        public_key = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        unpack_package(temporary, public_key)
        # Linking publishes atomically and refuses an existing destination on both platforms.
        destination.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def open_manifest(package: zipfile.ZipFile, public_key: bytes) -> dict[str, Any]:
    entries = package.infolist()
    if not 1 <= len(entries) <= MAX_FILES + 1 or len({entry.filename for entry in entries}) != len(
        entries
    ):
        raise ValueError("Недопустимый список файлов архива.")
    try:
        metadata = package.getinfo("release.json")
    except KeyError:
        raise ValueError("Описание обновления отсутствует.") from None
    if metadata.file_size > MAX_MANIFEST_BYTES or metadata.compress_type != zipfile.ZIP_STORED:
        raise ValueError("Недопустимое описание обновления.")
    envelope = json.loads(package.read(metadata), object_pairs_hook=unique_object)
    if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
        raise ValueError("Неподдерживаемая подпись обновления.")
    key = serialization.load_pem_public_key(public_key)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Требуется доверенный открытый ключ Ed25519.")
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        key.verify(signature, canonical(envelope["manifest"]))
    except (InvalidSignature, ValueError, TypeError):
        raise ValueError("Подпись пакета не соответствует доверенному ключу.") from None
    manifest: dict[str, Any] = envelope["manifest"]
    validate_manifest(manifest)
    expected = {"release.json", *("program/" + item["path"] for item in manifest["files"])}
    if {entry.filename for entry in entries} != expected:
        raise ValueError("Состав архива не соответствует подписанному описанию.")
    sizes = {"program/" + item["path"]: item["size"] for item in manifest["files"]}
    for entry in entries:
        mode = entry.external_attr >> 16
        if (
            entry.is_dir()
            or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG))
            or entry.flag_bits & 1
        ):
            raise ValueError("Архив может содержать только обычные незашифрованные файлы.")
        if entry.compress_type != zipfile.ZIP_STORED or entry.compress_size != entry.file_size:
            raise ValueError("Файлы пакета должны храниться без сжатия.")
        if entry.filename in sizes and entry.file_size != sizes[entry.filename]:
            raise ValueError("Размер файла не совпадает с подписанным описанием.")
    return manifest


def unpack_package(
    source: Path, public_key: bytes, destination: Path | None = None
) -> dict[str, Any]:
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > MAX_BYTES + 4 * MAX_MANIFEST_BYTES
    ):
        raise ValueError("Недопустимый файл обновления.")
    if destination is not None and (destination.exists() or destination.is_symlink()):
        raise ValueError("Пакет распаковывается только в новый каталог.")
    with zipfile.ZipFile(source) as package:
        manifest = open_manifest(package, public_key)
        # A private staging directory is published only after every byte has been checked.
        with tempfile.TemporaryDirectory(
            dir=destination.parent if destination else None
        ) as directory:
            staged = Path(directory) / "program"
            staged.mkdir()
            if destination is not None:
                for tree in (*PROGRAM_TREES, "infra"):
                    (staged / tree).mkdir(parents=True, exist_ok=True)
            for item in manifest["files"]:
                digest = hashlib.sha256()
                size = 0
                path = staged / item["path"]
                if destination is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                with package.open("program/" + item["path"]) as incoming:
                    if destination is None:
                        for block in iter(lambda: incoming.read(1024 * 1024), b""):
                            digest.update(block)
                            size += len(block)
                    else:
                        with path.open("xb") as output:
                            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                                digest.update(block)
                                size += len(block)
                                output.write(block)
                        path.chmod(0o644)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError("Содержимое файла не соответствует подписи пакета.")
            if destination is not None:
                staged.rename(destination)
    return manifest
