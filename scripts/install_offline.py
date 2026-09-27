"""Install a prepared bundle into a new directory without network requests or existing-data changes."""

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installation_options(project, ui_port, api_port, media_address):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("Invalid installation project name")
    if ui_port == api_port or any(
        not 1024 <= port <= 65535 for port in (ui_port, api_port)
    ):
        raise ValueError("Choose two different unprivileged TCP ports")
    address = ipaddress.ip_address(media_address)
    if address.version != 4 or not address.is_loopback:
        raise ValueError("Choose a local IPv4 loopback address for training media")
    return {
        "ports": [
            {
                "target": target,
                "published": str(port),
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
            }
            for target, port in ((5173, ui_port), (8000, api_port))
        ],
        "cors_origins": [
            f"https://localhost:{ui_port}",
            f"https://127.0.0.1:{ui_port}",
        ],
    }


def check_unused(project, ui_port, api_port, media_address):
    for command in (
        ["docker", "ps", "--all", "--quiet"],
        ["docker", "volume", "ls", "--quiet"],
        ["docker", "network", "ls", "--quiet"],
    ):
        if subprocess.check_output(
            command + ["--filter", f"label=com.docker.compose.project={project}"]
        ).strip():
            raise ValueError(
                "The selected project already has data; use its update workflow"
            )
    for address, port, kind in [
        *(("127.0.0.1", port, socket.SOCK_STREAM) for port in (ui_port, api_port)),
        *((media_address, port, socket.SOCK_DGRAM) for port in range(10000, 10200)),
    ]:
        with socket.socket(socket.AF_INET, kind) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((address, port))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--project", default="dispatcher112")
    parser.add_argument("--ui-port", type=int, default=5173)
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--media-address", default="127.0.0.1")
    args = parser.parse_args()
    route = installation_options(
        args.project, args.ui_port, args.api_port, args.media_address
    )
    bundle, target = args.bundle.resolve(), args.target.absolute()
    if target.resolve() != target or target.exists():
        raise ValueError(
            "The destination must be a new directory without symbolic links"
        )
    manifest_bytes = (bundle / "manifest.json").read_bytes()
    if (
        hashlib.sha256(manifest_bytes).hexdigest()
        != (bundle / "manifest.sha256").read_text(encoding="ascii").strip()
    ):
        raise ValueError("The delivery manifest failed its SHA-256 check")
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != "dispatcher-offline-1":
        raise ValueError("Unknown delivery format")
    names = [item["path"] for item in manifest["files"]]
    if len(set(names)) != len(names) or not {
        "source.zip",
        "images.tar",
        "images.json",
        "install_offline.py",
    }.issubset(names):
        raise ValueError("The delivery manifest is incomplete")
    if sys.version_info[:2] != (3, 11) or manifest.get("platform") != sys.platform:
        raise ValueError(
            "Use Python 3.11 and an offline bundle for this operating system"
        )
    check_unused(args.project, args.ui_port, args.api_port, args.media_address)
    for item in manifest["files"]:
        path = bundle / item["path"]
        if (
            path.resolve() != path
            or not path.is_relative_to(bundle)
            or digest(path) != item["sha256"]
        ):
            raise ValueError("A delivery file failed its SHA-256 check")
    if not (bundle / "images.tar").is_file() or not list(
        (bundle / "wheels").glob("*.whl")
    ):
        raise ValueError("The offline images or Python wheels are missing")
    with zipfile.ZipFile(bundle / "source.zip") as archive:
        names = set()
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.filename
                or ":" in member.filename
                or member.filename in names
                or member.external_attr >> 16 & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe or duplicate archive path")
            names.add(member.filename)
        required = sum(member.file_size for member in archive.infolist()) + 1024**3
        if shutil.disk_usage(target.parent).free < required:
            raise ValueError("Insufficient space for the installation")
        target.mkdir(parents=True)
        archive.extractall(target)
    subprocess.run([sys.executable, "-m", "venv", str(target / ".venv")], check=True)
    python = (
        target
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(bundle / "wheels"),
            "-r",
            str(target / "scripts/requirements-tls.txt"),
        ],
        check=True,
    )
    subprocess.run(
        ["docker", "load", "--input", str(bundle / "images.tar")], check=True
    )
    subprocess.run([str(python), "scripts/bootstrap.py"], cwd=target, check=True)
    environment = target / ".env"
    content = environment.read_text(encoding="utf-8")
    overrides = {
        "COMPOSE_PROJECT_NAME": args.project,
        "SIP_MEDIA_ADDRESS": args.media_address,
        "SIP_MEDIA_BIND": args.media_address,
    }
    lines = [
        line for line in content.splitlines() if line.partition("=")[0] not in overrides
    ]
    environment.write_text(
        "\n".join([*lines, *(f"{key}={value}" for key, value in overrides.items())])
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [str(python), "scripts/prepare_tls.py", "--activate"], cwd=target, check=True
    )
    image_path = target / "data/materials/.technical-host/offline-images.json"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundle / "images.json", image_path)
    (image_path.parent / "gateway-route.json").write_text(
        json.dumps(route), encoding="utf-8"
    )

    def compose(*arguments):
        subprocess.run(
            [str(python), "scripts/compose.py", *arguments], cwd=target, check=True
        )

    compose("up", "--detach", "--no-build", "--wait", "--wait-timeout", "240")
    for module in (
        "import_classifier",
        "import_streets",
        "create_demo_users",
        "manual_scenarios",
        "directory",
        "demo",
    ):
        compose(
            "run", "--rm", "--no-deps", "backend", "python", "-m", "app.seeds." + module
        )
    print(
        f"Installed: https://localhost:{args.ui_port}. Trust this installation's local CA as described in docs/TLS.md."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ):
        raise SystemExit(
            "Offline installation stopped; files and any created data are preserved."
        ) from None
