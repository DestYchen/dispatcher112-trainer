"""Trust this installation only, inside the isolated acceptance browser process."""

import base64
import hashlib
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

ROOT = Path(
    os.environ.get("DISPATCHER_INSTALLATION", Path(__file__).resolve().parents[1])
).resolve()
environment = ROOT / ".env"
tls_selected = environment.is_file() and any(
    line.startswith("COMPOSE_FILE=") and "docker-compose.tls.yml" in line
    for line in environment.read_text(encoding="utf-8").splitlines()
)
BASE_URL = os.environ.get(
    "DISPATCHER_UI_URL",
    "https://localhost:5173" if tls_selected else "http://localhost:5173",
).rstrip("/")


def browser_args() -> list[str]:
    if not BASE_URL.startswith("https://"):
        return []
    certificate = x509.load_pem_x509_certificate(
        (ROOT / ".secrets/pki/gateway/tls.crt").read_bytes()
    )
    public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pin = base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")
    return [f"--ignore-certificate-errors-spki-list={pin}"]


def launch_browser(playwright, **options):
    selected = os.environ.get("DISPATCHER_BROWSER", "chromium")
    if selected == "firefox":
        return playwright.firefox.launch(headless=True, **options)
    if selected == "chrome":
        options["channel"] = "chrome"
    elif selected == "yandex":
        candidates = [
            Path(os.environ.get(key, "")) / relative
            for key, relative in (
                ("PROGRAMFILES", "Yandex/YandexBrowser/Application/browser.exe"),
                ("PROGRAMFILES(X86)", "Yandex/YandexBrowser/Application/browser.exe"),
                ("LOCALAPPDATA", "Yandex/YandexBrowser/Application/browser.exe"),
            )
        ]
        options["executable_path"] = str(
            next(path for path in candidates if path.is_file())
        )
    elif selected != "chromium":
        raise ValueError("Unknown acceptance browser")
    return playwright.chromium.launch(headless=True, args=browser_args(), **options)


def context_options():
    # Isolated Firefox/Yandex profiles have no local CA (Yandex also ignores
    # Chromium's SPKI switch). This exception is confined to the test context;
    # the HTTP acceptance client separately validates the CA and hostname.
    return (
        {"ignore_https_errors": True}
        if os.environ.get("DISPATCHER_BROWSER") in {"firefox", "yandex"}
        else {}
    )


if BASE_URL.startswith("https://"):
    # Playwright's Node HTTP client validates the CA and hostname normally.
    os.environ["NODE_EXTRA_CA_CERTS"] = str(ROOT / ".secrets/pki/ca.crt")
