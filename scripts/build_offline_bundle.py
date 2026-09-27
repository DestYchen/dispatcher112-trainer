"""Collect source, public training assets, pinned Docker images and offline Python wheels."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import software_package

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.operations.update_package import release_files  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "delivery/2026-09-27/offline"
    )
    refresh = parser.add_mutually_exclusive_group()
    refresh.add_argument("--refresh-source", action="store_true")
    refresh.add_argument(
        "--refresh-images",
        action="store_true",
        help="Refresh source and images, reuse the existing offline wheels",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "delivery"):
        raise ValueError("Place the bundle inside delivery/")
    output.mkdir(parents=True, exist_ok=True)
    if args.refresh_images and not list((output / "wheels").glob("*.whl")):
        raise ValueError("Prepare the offline wheels before refreshing images")
    if args.refresh_source:
        if (
            not (output / "manifest.json").is_file()
            or not (output / "images.tar").is_file()
        ):
            raise ValueError("Finish the original bundle before refreshing its source")
        images = json.loads((output / "images.json").read_text(encoding="utf-8"))
        needed = 5 * 1024**3
    else:
        images = software_package.inventory()
        details = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", *sorted(set(images.values()))]
            )
        )
        needed = sum(image["Size"] for image in details) + 5 * 1024**3
    if shutil.disk_usage(output).free < needed:
        raise ValueError("Insufficient disk space for the offline bundle")
    (output / "images.json").write_text(json.dumps(images, indent=2), encoding="utf-8")
    paths = set(release_files(ROOT))
    paths.add(ROOT / "PROGRESS.md")
    for name in ("data/README.md", "data/classifier.xlsx", "data/streets.csv"):
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    for name in ("data/fonts", "data/voices", "data/llm/models", "data/tickets"):
        paths.update(path for path in (ROOT / name).rglob("*") if path.is_file())
    for name in ("data/llm/manifest.json", "data/llm/Modelfile.txt"):
        paths.add(ROOT / name)
    source = output / "source.zip"
    with zipfile.ZipFile(
        source.with_suffix(".partial"), "w", allowZip64=True
    ) as archive:
        for path in sorted(paths):
            if path.resolve() != path or not path.is_relative_to(ROOT):
                raise ValueError("Delivery sources cannot contain links")
            archive.write(
                path,
                path.relative_to(ROOT).as_posix(),
                compress_type=zipfile.ZIP_STORED
                if path.stat().st_size > 4 * 1024**2
                else zipfile.ZIP_DEFLATED,
            )
    source.with_suffix(".partial").replace(source)
    print("Source and public training assets collected", flush=True)
    wheels = output / "wheels"
    wheels.mkdir(exist_ok=True)
    if not args.refresh_source and not args.refresh_images:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--dest",
                str(wheels),
                "-r",
                str(ROOT / "scripts/requirements-tls.txt"),
                "-r",
                str(ROOT / "scripts/requirements-delivery.txt"),
            ],
            check=True,
        )
    if not args.refresh_source:
        print("Saving exact local Docker images", flush=True)
        image_archive = output / "images.tar"
        subprocess.run(
            [
                "docker",
                "save",
                "--output",
                str(image_archive.with_suffix(".partial")),
                *sorted(set(images.values())),
            ],
            check=True,
        )
        image_archive.with_suffix(".partial").replace(image_archive)
    shutil.copyfile(ROOT / "scripts/install_offline.py", output / "install_offline.py")
    shutil.copyfile(ROOT / "docs/DELIVERY.md", output / "README.md")
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "manifest.sha256"}:
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": checksum,
                }
            )
    manifest = output / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "dispatcher-offline-1",
                "platform": sys.platform,
                "python": "3.11",
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output / "manifest.sha256").write_text(
        hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n", encoding="ascii"
    )
    print(f"Offline bundle complete: {len(files)} files", flush=True)


if __name__ == "__main__":
    main()
