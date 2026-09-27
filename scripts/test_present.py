import base64
import hashlib
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from prepare_tls import prepare
from present import browser_command, presentation_url


def test_presentation_uses_configured_gateway_port(tmp_path):
    assert presentation_url(tmp_path) == "https://localhost:5173"
    route = tmp_path / "data/materials/.technical-host/gateway-route.json"
    route.parent.mkdir(parents=True)
    route.write_text(json.dumps({"ports": [{"target": 5173, "published": 25173}]}))
    assert presentation_url(tmp_path) == "https://localhost:25173"


def test_presentation_isolates_accounts_and_trusts_only_installation_key(tmp_path):
    pki = tmp_path / ".secrets/pki"
    prepare(pki, [])
    cert = x509.load_pem_x509_certificate((pki / "gateway/tls.crt").read_bytes())
    key = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pin = base64.b64encode(hashlib.sha256(key).digest()).decode("ascii")
    profiles = set()
    for role in ("teacher", "student"):
        command = browser_command(
            tmp_path, "chrome.exe", role, "https://localhost:25173"
        )
        assert f"--ignore-certificate-errors-spki-list={pin}" in command
        assert "--ignore-certificate-errors" not in command
        assert f"--app=https://localhost:25173/?showcase={role}" in command
        profiles.add(next(arg for arg in command if arg.startswith("--user-data-dir=")))
    assert len(profiles) == 2


@pytest.mark.parametrize("role", ["admin", "../other"])
def test_presentation_rejects_unknown_role_before_accessing_profile(tmp_path, role):
    with pytest.raises(ValueError, match="Unknown presentation role"):
        browser_command(tmp_path, "chrome.exe", role, "https://localhost:5173")
