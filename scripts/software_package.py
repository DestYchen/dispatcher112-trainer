"""Build, verify and stage signed offline releases without changing the running installation."""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import recover_snapshot as recovery

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.operations.recovery_compose import SERVICES  # noqa: E402
from app.operations.snapshot import file_digest  # noqa: E402
from app.operations.update_package import (  # noqa: E402
    MAX_BYTES,
    MAX_MANIFEST_BYTES,
    create_package,
    release_files,
    unpack_package,
)


def write_new(path, data, mode=0o600):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def windows_command(*arguments):
    result = subprocess.run(arguments, capture_output=True)
    if result.returncode:
        raise RuntimeError("Unable to protect the publisher key directory")
    # Native Windows utilities can use an OEM code page. Only the ASCII SID is consumed.
    return result.stdout.decode("utf-8", errors="replace")


def create_signer(directory):
    directory = Path(directory).absolute()
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("Signing directory must not contain symbolic links")
    directory.mkdir(mode=0o700)
    if sys.platform == "win32":
        identity = next(
            csv.reader(
                io.StringIO(windows_command("whoami", "/user", "/fo", "csv", "/nh"))
            )
        )
        sid = identity[1]
        if not sid.startswith("S-1-") or not all(c.isdigit() or c in "S-" for c in sid):
            raise ValueError("Unable to establish the signing directory owner")
        windows_command(
            "icacls", str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"
        )
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    write_new(directory / "publisher.key", private)
    write_new(directory / "publisher.pub", public, 0o644)
    return {"public_key_sha256": hashlib.sha256(public).hexdigest()}


def inventory():
    source = json.loads(
        recovery.run(
            "docker", "compose", "--profile", "*", "config", "--format", "json"
        )
    )
    images = recovery.installed_images(source)
    if set(images) != SERVICES:
        raise ValueError("Unsupported installed service composition")
    return images


def checked_target(value):
    original = Path(value).absolute()
    target = original.resolve()
    directory = (ROOT / ".updates").resolve()
    if not target.is_relative_to(directory) or target == directory:
        raise ValueError("Staging target must be a new directory below .updates")
    if any(path.is_symlink() for path in (original, *original.parents)):
        raise ValueError("Staging paths must not contain symbolic links")
    if target.exists():
        raise ValueError("Staging target already exists")
    return target


def prepare(archive, public_key, target):
    target = checked_target(target)
    # Verify the archive before creating directories or inspecting Docker resources.
    manifest = unpack_package(archive, public_key)
    required = 2 * sum(item["size"] for item in manifest["files"]) + 256 * 1024 * 1024
    if shutil.disk_usage(ROOT).free < required:
        raise ValueError("Insufficient space for staging the software package")
    for image in sorted(set(manifest["images"].values())):
        actual = recovery.run(
            "docker", "image", "inspect", "--format", "{{.Id}}", image
        )
        if actual != image:
            raise ValueError(
                "A required local image is unavailable; install offline images first"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        staged = Path(temporary) / "release"
        staged.mkdir()
        saved_archive = staged / "release.zip"
        with archive.open("rb") as incoming, saved_archive.open("xb") as output:
            copied = 0
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                copied += len(block)
                if copied > MAX_BYTES + 4 * MAX_MANIFEST_BYTES:
                    raise ValueError("Package size changed during preparation")
                output.write(block)
        workspace = staged / "program"
        verified = unpack_package(saved_archive, public_key, workspace)
        if verified != manifest:
            raise ValueError("Package changed during preparation")
        recovery.check_dependencies(manifest["images"], workspace)
        state = {
            "phase": "VERIFIED",
            "version": manifest["version"],
            "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
            "package_sha256": file_digest(saved_archive),
            "manifest": manifest,
        }
        write_new(
            staged / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2).encode(),
        )
        staged.rename(target)
    return state


def integrity(archive, public_key, root):
    manifest = unpack_package(archive, public_key)
    expected = {item["path"]: item for item in manifest["files"]}
    missing, changed = [], []
    for name, item in expected.items():
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            changed.append(name)
        elif not path.is_file():
            missing.append(name)
        elif path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]:
            changed.append(name)
    # Inventory rejects secret files, links and absent mandatory trees as an integrity failure.
    try:
        actual = {p.relative_to(root).as_posix() for p in release_files(root)}
        extra = sorted(actual - expected.keys())
        invalid_inventory = False
    except ValueError:
        extra = []
        invalid_inventory = True
    return {
        "version": manifest["version"],
        "passed": not (missing or changed or extra or invalid_inventory),
        "missing": sorted(missing),
        "changed": sorted(changed),
        "extra": extra,
        "invalid_inventory": invalid_inventory,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    keys = commands.add_parser(
        "keys", help="Create a publisher key pair in a new private directory"
    )
    keys.add_argument("--directory", type=Path, required=True)
    images = commands.add_parser(
        "images", help="Record exact image IDs from the local installation"
    )
    images.add_argument("--output", type=Path, required=True)
    build = commands.add_parser("build", help="Build a signed program archive")
    build.add_argument("--signing-key", type=Path, required=True)
    build.add_argument("--images", type=Path, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--description", required=True)
    build.add_argument("--output", type=Path, required=True)
    for name in ("verify", "prepare", "integrity"):
        command = commands.add_parser(name)
        command.add_argument("--package", type=Path, required=True)
        command.add_argument("--trust-key", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--target", type=Path, required=True)
        if name == "integrity":
            command.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args()
    try:
        if arguments.command == "keys":
            result = create_signer(arguments.directory)
        elif arguments.command == "images":
            result = inventory()
            write_new(arguments.output, json.dumps(result, indent=2).encode())
        elif arguments.command == "build":
            result = create_package(
                ROOT,
                arguments.output,
                arguments.signing_key.read_bytes(),
                arguments.version,
                arguments.description,
                json.loads(arguments.images.read_text(encoding="utf-8")),
            )
        else:
            public_key = arguments.trust_key.read_bytes()
            if arguments.command == "prepare":
                result = prepare(arguments.package, public_key, arguments.target)
            elif arguments.command == "integrity":
                result = integrity(
                    arguments.package, public_key, arguments.root.resolve()
                )
            else:
                result = unpack_package(arguments.package, public_key)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 1 if result.get("passed") is False else 0
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"Software package operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
