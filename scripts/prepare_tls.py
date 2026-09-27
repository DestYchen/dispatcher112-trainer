"""Prepare an installation-local CA and isolated service keys; never alter OS trust."""

import argparse
import ipaddress
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVICES = (
    "backend",
    "worker",
    "sip_worker",
    "telephony",
    "gateway",
    "frontend",
    "postgres",
    "redis",
    "languagetool",
    "ollama",
    "control",
    "backup",
)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Certificate paths must not be symbolic links")
    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def private_bytes(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def prepare(
    root: Path, hosts: list[str], *, repair_ca_extensions: bool = False
) -> dict[str, str]:
    if root.is_symlink() or any(
        (root / name).is_symlink() for name in ("private", *SERVICES)
    ):
        raise ValueError("Certificate directories must not be symbolic links")
    alternatives: list[x509.GeneralName] = []
    for host in sorted(set(["localhost", "127.0.0.1", "::1", *hosts])):
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if (
                not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host)
                or len(host) > 253
            ):
                raise ValueError(
                    "Use a DNS name or IP address without scheme, port or path"
                )
            alternatives.append(x509.DNSName(host))
    now = datetime.now(UTC)
    ca_path, key_path = root / "ca.crt", root / "private/ca.key"
    if ca_path.is_symlink() or key_path.is_symlink():
        raise ValueError("CA paths must not be symbolic links")
    if ca_path.exists() != key_path.exists():
        raise ValueError(
            "Incomplete CA: restore the matching certificate and private key"
        )
    if ca_path.exists():
        ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
        if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("Existing certificate is not a CA")
        ca.verify_directly_issued_by(ca)
        loaded = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError("Expected RSA CA key")
        ca_key = loaded
        public = ca.public_key()
        if (
            not isinstance(public, rsa.RSAPublicKey)
            or public.public_numbers() != ca_key.public_key().public_numbers()
        ):
            raise ValueError("CA key and certificate do not match")
        if ca.not_valid_after_utc < now + timedelta(days=366):
            raise ValueError(
                "CA expires within a year; arrange CA renewal and client trust explicitly"
            )
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Dispatcher112 installation CA")]
        )
        ca = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        atomic_write(key_path, private_bytes(ca_key), 0o600)
        atomic_write(ca_path, ca.public_bytes(serialization.Encoding.PEM), 0o644)
    identifiers = [
        x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
    ]
    missing = [
        item
        for item in identifiers
        if item.oid not in {ext.oid for ext in ca.extensions}
    ]
    if missing:
        if not repair_ca_extensions:
            raise ValueError(
                "Existing CA lacks RFC 5280 key identifiers; use --repair-ca-extensions "
                "to reissue its public certificate with the SAME private key"
            )
        builder = (
            x509.CertificateBuilder()
            .subject_name(ca.subject)
            .issuer_name(ca.issuer)
            .public_key(ca_key.public_key())
            .serial_number(ca.serial_number)
            .not_valid_before(ca.not_valid_before_utc)
            .not_valid_after(ca.not_valid_after_utc)
        )
        for extension in ca.extensions:
            builder = builder.add_extension(extension.value, extension.critical)
        for identifier in missing:
            builder = builder.add_extension(identifier, critical=False)
        atomic_write(
            root / "ca-before-extensions.crt",
            ca.public_bytes(serialization.Encoding.PEM),
            0o644,
        )
        ca = builder.sign(ca_key, hashes.SHA256())
        atomic_write(ca_path, ca.public_bytes(serialization.Encoding.PEM), 0o644)
    for service in SERVICES:
        directory = root / service
        certificate_path = directory / "tls.crt"
        service_key_path = directory / "tls.key"
        names = [x509.DNSName(service), *alternatives]
        if certificate_path.exists() and service_key_path.exists():
            existing = x509.load_pem_x509_certificate(certificate_path.read_bytes())
            loaded_key = serialization.load_pem_private_key(
                service_key_path.read_bytes(), password=None
            )
            old_public = existing.public_key()
            if (
                not isinstance(loaded_key, rsa.RSAPrivateKey)
                or not isinstance(old_public, rsa.RSAPublicKey)
                or loaded_key.public_key().public_numbers()
                != old_public.public_numbers()
            ):
                raise ValueError(f"Certificate and key do not match: {service}")
            existing.verify_directly_issued_by(ca)
            previous = existing.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            identifiers_present = all(
                identifier.oid in {ext.oid for ext in existing.extensions}
                for identifier in identifiers
            )
            if (
                existing.not_valid_after_utc > now + timedelta(days=30)
                and all(name in previous for name in names)
                and identifiers_present
            ):
                atomic_write(
                    directory / "ca.crt",
                    ca.public_bytes(serialization.Encoding.PEM),
                    0o644,
                )
                continue
            # Renewal must retain names already configured for classroom access.
            names.extend(name for name in previous if name not in names)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, service)]))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        atomic_write(service_key_path, private_bytes(key), 0o600)
        atomic_write(
            certificate_path,
            certificate.public_bytes(serialization.Encoding.PEM),
            0o644,
        )
        atomic_write(
            directory / "ca.crt", ca.public_bytes(serialization.Encoding.PEM), 0o644
        )
    return {
        "ca_sha256": ca.fingerprint(hashes.SHA256()).hex(),
        "valid_until": ca.not_valid_after_utc.isoformat(),
    }


def activate(environment: Path) -> None:
    """Persist the overlay so a later ordinary Compose command cannot remove TLS."""
    content = environment.read_text(encoding="utf-8")
    settings = {
        "COMPOSE_PATH_SEPARATOR": ";",
        "COMPOSE_FILE": "docker-compose.yml;docker-compose.tls.yml",
    }
    lines = [
        line
        for line in content.splitlines()
        if line.split("=", 1)[0].strip() not in settings
    ]
    lines.extend(f"{key}={value}" for key, value in settings.items())
    atomic_write(environment, ("\n".join(lines) + "\n").encode("utf-8"), 0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", action="append", default=[])
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--repair-ca-extensions", action="store_true")
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    root = workspace / ".secrets/pki"
    if not root.resolve().is_relative_to(workspace):
        raise ValueError("Certificate directory must stay inside the workspace")
    result = prepare(root, args.host, repair_ca_extensions=args.repair_ca_extensions)
    if args.activate:
        activate(workspace / ".env")
        print("TLS overlay selected for subsequent Docker Compose commands.")
    print("Local service certificates prepared; OS trust unchanged.")
    print("CA SHA256:", result["ca_sha256"])


if __name__ == "__main__":
    main()
