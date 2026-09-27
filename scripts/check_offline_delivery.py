"""Verify a fresh local installation against its offline source and image manifest."""

import argparse
import hashlib
import io
import json
import subprocess
import zipfile
from contextlib import closing
from pathlib import Path

from openpyxl import load_workbook

from check_recovery_persistence import api, login

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--bundle", type=Path, default=ROOT / "delivery/2026-09-27/offline"
    )
    args = parser.parse_args()
    target = args.installation.resolve()
    assert target.is_relative_to(ROOT) and target != ROOT
    source_count = 0
    with zipfile.ZipFile(args.bundle / "source.zip") as source:
        for item in source.infolist():
            assert not any(
                part in {".secrets", ".env", "id_ed25519", "id_rsa"}
                for part in Path(item.filename).parts
            )
            with (
                source.open(item) as expected,
                (target / item.filename).open("rb") as actual,
            ):
                assert (
                    hashlib.file_digest(expected, "sha256").digest()
                    == hashlib.file_digest(actual, "sha256").digest()
                ), item.filename
            source_count += 1
    ids = subprocess.check_output(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={args.project}",
        ],
        text=True,
    ).split()
    assert ids
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids]))
    images = json.loads((args.bundle / "images.json").read_text(encoding="utf-8"))
    services = {}
    for row in containers:
        service = row["Config"]["Labels"]["com.docker.compose.service"]
        assert row["Image"] == images[service], service
        assert (
            row["State"]["Running"]
            and row["State"].get("Health", {"Status": "healthy"})["Status"] == "healthy"
        ), service
        assert row["HostConfig"]["ReadonlyRootfs"], service
        services[service] = row["Image"]
    assert {
        "backend",
        "worker",
        "sip_worker",
        "gateway",
        "frontend",
        "postgres",
        "redis",
        "backup",
        "telephony",
        "ollama",
        "ollama_tls",
        "languagetool",
        "languagetool_tls",
    } == set(services)
    with closing(login(args.url, target, "teacher", "teacher")) as teacher:
        lesson = api(teacher, "/teacher/lessons")["items"][0]
        prefix = f"/teacher/lessons/{lesson['id']}/report"
        data = api(teacher, prefix)
        exported = {}
        for extension in ("pdf", "csv", "xml", "xlsx"):
            response = teacher.get(prefix + "." + extension)
            response.raise_for_status()
            assert response.content and response.headers["cache-control"] == "no-store"
            exported[extension] = len(response.content)
            if extension == "pdf":
                assert response.content.startswith(b"%PDF-")
            elif extension == "xlsx":
                sheet = load_workbook(io.BytesIO(response.content)).active
                assert (
                    sheet is not None
                    and [cell.value for cell in sheet[1]] == data["columns"]
                )
    result = {
        "passed": True,
        "manifest_sha256": hashlib.sha256(
            (args.bundle / "manifest.json").read_bytes()
        ).hexdigest(),
        "project": args.project,
        "url": args.url,
        "source_files_verified": source_count,
        "running_services": len(services),
        "images_verified": len(services),
        "read_only_roots": True,
        "tls_ca_and_hostname_verified": True,
        "exports_bytes": exported,
    }
    (ROOT / "artifacts/ui-review/full-tz-offline-installation.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
