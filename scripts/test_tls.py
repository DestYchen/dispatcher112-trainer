import re
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from prepare_tls import SERVICES, activate, prepare
from backend.app.operations.runtime_files import SERVICES as RUNTIME_SERVICES


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pki")
    prepare(path, ["classroom.local", "192.168.10.20"])
    return path


def handshake(
    server_context: ssl.SSLContext, client_context: ssl.SSLContext, hostname: str
) -> None:
    server_in, server_out, client_in, client_out = [ssl.MemoryBIO() for _ in range(4)]
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server_done = client_done = False
    for _ in range(20):
        try:
            client.do_handshake()
            client_done = True
        except ssl.SSLWantReadError:
            client_done = False
        server_in.write(client_out.read())
        try:
            server.do_handshake()
            server_done = True
        except ssl.SSLWantReadError:
            server_done = False
        client_in.write(server_out.read())
        if server_done and client_done:
            assert client.version() in {"TLSv1.2", "TLSv1.3"}
            return
    raise AssertionError("TLS handshake did not finish")


def test_every_service_has_individual_key_signed_san_and_verified_tls(
    pki: Path,
) -> None:
    ca = x509.load_pem_x509_certificate((pki / "ca.crt").read_bytes())
    keys = set()
    client = ssl.create_default_context(cafile=str(pki / "ca.crt"))
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    for service in SERVICES:
        cert = x509.load_pem_x509_certificate((pki / service / "tls.crt").read_bytes())
        cert.verify_directly_issued_by(ca)
        assert not cert.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value.ca
        keys.add((pki / service / "tls.key").read_bytes())
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.minimum_version = ssl.TLSVersion.TLSv1_2
        server.load_cert_chain(
            str(pki / service / "tls.crt"), str(pki / service / "tls.key")
        )
        handshake(server, client, service)
        handshake(server, client, "classroom.local")
        with pytest.raises(ssl.SSLCertVerificationError):
            handshake(server, client, "wrong-host.local")
    assert len(keys) == len(SERVICES)


def test_untrusted_certificate_cannot_connect(pki: Path) -> None:
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(pki / "backend/tls.crt"), str(pki / "backend/tls.key"))
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(server, ssl.create_default_context(), "backend")


def test_compose_certificate_mounts_have_prepared_keys(pki: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    overlay = root / "docker-compose.tls.yml"
    mounted = set(
        re.findall(r"\.secrets/pki/([a-z_]+):/run/tls:ro", overlay.read_text())
    )
    base = (root / "docker-compose.yml").read_text(encoding="utf-8")
    copied = set(
        re.findall(
            r"runtime_([a-z_]+):/run(?:/credentials)?:ro", base + overlay.read_text()
        )
    )
    prepared = set(re.findall(r"runtime_([a-z_]+):/prepared/[a-z_]+", base))
    assert copied == prepared == set(RUNTIME_SERVICES)
    assert mounted | copied == set(SERVICES)
    for service in mounted | copied:
        assert (pki / service / "tls.crt").is_file()
        assert (pki / service / "tls.key").is_file()


def test_preparation_preserves_valid_keys_and_ca(pki: Path) -> None:
    before = {str(path): path.read_bytes() for path in pki.rglob("*") if path.is_file()}
    result = prepare(pki, ["classroom.local", "192.168.10.20"])
    after = {str(path): path.read_bytes() for path in pki.rglob("*") if path.is_file()}
    assert before == after
    ca = x509.load_pem_x509_certificate((pki / "ca.crt").read_bytes())
    assert result["ca_sha256"] == ca.fingerprint(hashes.SHA256()).hex()


@pytest.mark.parametrize(
    "host", ["https://example.local", "host:443", "../host", "bad host", "*"]
)
def test_invalid_hostnames_are_rejected_before_creating_keys(
    tmp_path: Path, host: str
) -> None:
    with pytest.raises(ValueError):
        prepare(tmp_path / "pki", [host])
    assert not (tmp_path / "pki").exists()


def test_partial_ca_is_not_silently_replaced(tmp_path: Path) -> None:
    (tmp_path / "ca.crt").write_bytes(b"existing certificate")
    with pytest.raises(ValueError, match="Incomplete CA"):
        prepare(tmp_path, [])
    assert (tmp_path / "ca.crt").read_bytes() == b"existing certificate"


def test_activation_preserves_secrets_and_is_idempotent(tmp_path: Path) -> None:
    environment = tmp_path / ".env"
    environment.write_text(
        "# Keep user configuration\nJWT_SECRET=example-only\n"
        "COMPOSE_FILE=old.yml\nPOSTGRES_DB=existing\n",
        encoding="utf-8",
    )
    activate(environment)
    first = environment.read_bytes()
    activate(environment)
    assert environment.read_bytes() == first
    content = first.decode("utf-8")
    assert "JWT_SECRET=example-only\n" in content
    assert "POSTGRES_DB=existing\n" in content
    assert content.count("COMPOSE_FILE=") == 1
    assert "COMPOSE_FILE=docker-compose.yml;docker-compose.tls.yml\n" in content
    assert "COMPOSE_PATH_SEPARATOR=;\n" in content


def test_activation_requires_existing_bootstrap_configuration(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        activate(tmp_path / ".env")


def test_legacy_ca_upgrade_is_explicit_and_keeps_its_private_key(
    tmp_path: Path,
) -> None:
    prepare(tmp_path, ["classroom.local"])
    certificate = x509.load_pem_x509_certificate((tmp_path / "ca.crt").read_bytes())
    key_bytes = (tmp_path / "private/ca.key").read_bytes()
    key = serialization.load_pem_private_key(key_bytes, password=None)
    builder = (
        x509.CertificateBuilder()
        .subject_name(certificate.subject)
        .issuer_name(certificate.issuer)
        .public_key(certificate.public_key())
        .serial_number(certificate.serial_number)
        .not_valid_before(certificate.not_valid_before_utc)
        .not_valid_after(certificate.not_valid_after_utc)
    )
    for extension in certificate.extensions:
        if not isinstance(
            extension.value, (x509.SubjectKeyIdentifier, x509.AuthorityKeyIdentifier)
        ):
            builder = builder.add_extension(extension.value, extension.critical)
    legacy = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    (tmp_path / "ca.crt").write_bytes(legacy)
    with pytest.raises(ValueError, match="repair-ca-extensions"):
        prepare(tmp_path, [])
    assert (tmp_path / "ca.crt").read_bytes() == legacy
    prepare(tmp_path, [], repair_ca_extensions=True)
    assert (tmp_path / "private/ca.key").read_bytes() == key_bytes
    assert (tmp_path / "ca-before-extensions.crt").read_bytes() == legacy
    client = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(
        str(tmp_path / "backend/tls.crt"), str(tmp_path / "backend/tls.key")
    )
    handshake(server, client, "classroom.local")
