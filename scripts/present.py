"""Start the installed simulator and open isolated teacher/student presentation windows."""

import argparse
import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

import compose

ROOT = Path(__file__).resolve().parents[1]


def presentation_url(root):
    route = root / "data/materials/.technical-host/gateway-route.json"
    if route.is_file():
        ports = json.loads(route.read_text(encoding="utf-8"))["ports"]
        port = next(int(item["published"]) for item in ports if item["target"] == 5173)
    else:
        port = 5173
    return f"https://localhost:{port}"


def browser_command(root, executable, role, url):
    if role not in {"teacher", "student"}:
        raise ValueError("Unknown presentation role")
    certificate = x509.load_pem_x509_certificate(
        (root / ".secrets/pki/gateway/tls.crt").read_bytes()
    )
    public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pin = base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")
    return [
        str(executable),
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        f"--user-data-dir={root / '.presentation' / role}",
        f"--ignore-certificate-errors-spki-list={pin}",
        f"--app={url}/?showcase={role}",
    ]


def find_browser():
    for folder in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        for relative in (
            "Google/Chrome/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
        ):
            path = Path(os.environ.get(folder, "")) / relative
            if path.is_file():
                return path
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not (ROOT / ".secrets/pki/ca.crt").is_file():
        raise ValueError(
            "First install the simulator using README.md or the offline installer."
        )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/compose.py"),
            "up",
            "--detach",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "240",
        ],
        cwd=ROOT,
        check=True,
    )
    url = presentation_url(ROOT)
    context = ssl.create_default_context(cafile=ROOT / ".secrets/pki/ca.crt")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(url + "/api/v1/auth/me", timeout=10):
            raise ValueError("The demonstration requires the normal sign-in screen")
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        if json.loads(error.read())["error"]["code"] != "UNAUTHENTICATED":
            raise ValueError(
                "Unexpected sign-in response; check the installed gateway"
            ) from None
    completed = subprocess.run(
        compose.command(
            ROOT,
            [
                "run",
                "--rm",
                "--no-deps",
                "backend",
                "python",
                "-m",
                "app.seeds.showcase",
            ],
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    result = json.loads(completed.stdout)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    print(
        f"Ready: {url}\nAccounts: demo.teacher / demo.student\nTraining password: Showcase112!",
        flush=True,
    )
    if args.prepare_only:
        return
    executable = find_browser()
    if executable is None:
        print("Open the URL in two separate browser profiles; see docs/SHOWCASE.md.")
        return
    for role in ("teacher", "student"):
        subprocess.Popen(
            browser_command(ROOT, executable, role, url),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(
        "Two presentation windows opened. Sign in, then start exercise 01 in the teacher window."
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"Presentation did not start: {error}. Check Docker Desktop and docs/SHOWCASE.md."
        ) from None
