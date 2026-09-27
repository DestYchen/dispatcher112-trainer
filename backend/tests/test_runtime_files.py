import os
import stat
from pathlib import Path

import pytest

from app.operations.backup_protocol import read_signed, write_signed
from app.operations.runtime_files import GID, SERVICES, UID, copy_secret, prepare, writable_tree
from tests.test_update_package import signing_keys


def sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    secrets, data, prepared = (tmp_path / name for name in ("secrets", "data", "prepared"))
    for root in (secrets, data, prepared):
        root.mkdir()
    for service in SERVICES:
        directory = secrets / "pki" / service
        directory.mkdir(parents=True)
        for name in ("ca.crt", "tls.crt", "tls.key"):
            path = directory / name
            path.write_text(f"test-{service}-{name}")
            path.chmod(0o600)
    for name in ("control", "backup-control", "private-ca"):
        directory = secrets / name
        directory.mkdir()
        (directory / "token").write_text("test-key-do-not-expose-to-other-services")
    return secrets, data, prepared


def test_credentials_are_isolated_owned_and_refreshed_without_changing_sources(
    tmp_path: Path,
) -> None:
    secrets, data, prepared = sources(tmp_path)
    source = secrets / "pki/backend/tls.key"
    before = source.stat()
    prepare(secrets, data, prepared)
    for service in SERVICES:
        root = prepared / service
        assert root.stat().st_uid == UID
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        expected = {"tls/ca.crt", "tls/tls.crt", "tls/tls.key"}
        if service == "backend":
            expected |= {"control/token", "backup-control/token"}
        assert {
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
        } == expected
        for path in root.rglob("*"):
            assert path.stat().st_uid == UID and path.stat().st_gid == GID
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
    after = source.stat()
    assert (before.st_uid, before.st_gid, before.st_mode, before.st_mtime_ns) == (
        after.st_uid,
        after.st_gid,
        after.st_mode,
        after.st_mtime_ns,
    )
    source.write_text("renewed-test-certificate-key")
    feedback = data / "feedback.jsonl"
    feedback.write_text("existing feedback\n")
    modified = feedback.stat().st_mtime_ns
    prepare(secrets, data, prepared)
    assert (prepared / "backend/tls/tls.key").read_text() == source.read_text()
    assert feedback.read_text() == "existing feedback\n" and feedback.stat().st_mtime_ns == modified


def test_base_http_installation_does_not_require_tls_but_requires_control_keys(
    tmp_path: Path,
) -> None:
    secrets, data, prepared = (tmp_path / name for name in ("secrets", "data", "prepared"))
    for root in (secrets, data, prepared):
        root.mkdir()
    for name in ("control", "backup-control"):
        (secrets / name).mkdir()
        (secrets / name / "token").write_text("test-only-token")
    prepare(secrets, data, prepared)
    assert not (prepared / "backend/tls").exists()
    (secrets / "backup-control/token").unlink()
    with pytest.raises(ValueError, match="credential"):
        prepare(secrets, data, prepared)


def test_optional_publisher_key_is_isolated_and_removal_revokes_runtime_copy(
    tmp_path: Path,
) -> None:
    secrets, data, prepared = sources(tmp_path)
    public = signing_keys()[1]
    (secrets / "software-publisher.pub").write_bytes(public)
    prepare(secrets, data, prepared)
    copied = prepared / "backend/software-publisher.pub"
    assert copied.read_bytes() == public
    assert copied.stat().st_uid == UID and stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert all(
        not (prepared / name / "software-publisher.pub").exists()
        for name in SERVICES
        if name != "backend"
    )
    (secrets / "software-publisher.pub").unlink()
    prepare(secrets, data, prepared)
    assert not copied.exists()


def test_invalid_optional_publisher_key_is_not_copied(tmp_path: Path) -> None:
    secrets, data, prepared = sources(tmp_path)
    (secrets / "software-publisher.pub").write_bytes(b"not a trusted public key")
    with pytest.raises(ValueError):
        prepare(secrets, data, prepared)
    assert not (prepared / "backend/software-publisher.pub").exists()


@pytest.mark.parametrize("location", ["source", "target", "parent"])
def test_credential_symlinks_do_not_read_or_overwrite_external_files(
    tmp_path: Path, location: str
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("unchanged-external-value")
    original_mode = outside.stat().st_mode
    credential = source / "key"
    credential.write_text("test-credential")
    destination = target / "tls" / "key"
    if location == "source":
        credential.unlink()
        credential.symlink_to(outside)
    elif location == "target":
        destination.parent.mkdir()
        destination.symlink_to(outside)
    else:
        destination.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        copy_secret(credential, destination, source, target)
    assert outside.read_text() == "unchanged-external-value"
    assert outside.stat().st_mode == original_mode


def test_data_preparation_refuses_symlinks_before_changing_file_ownership(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    folder = data / "materials"
    folder.mkdir()
    regular = folder / "regular"
    regular.write_text("data")
    original = regular.stat()
    (folder / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        writable_tree(folder, data)
    assert regular.stat().st_uid == original.st_uid and regular.stat().st_mode == original.st_mode


def test_existing_data_and_new_signed_results_are_readable_by_the_runtime_group(
    tmp_path: Path,
) -> None:
    secrets, data, prepared = sources(tmp_path)
    recordings = data / "telephony/recordings"
    recordings.mkdir(parents=True)
    recording = recordings / "old.wav"
    recording.write_bytes(b"RIFF-test")
    recording.chmod(0o600)
    prepare(secrets, data, prepared)
    assert recording.read_bytes() == b"RIFF-test"
    assert recording.stat().st_uid == UID and recording.stat().st_gid == GID
    assert stat.S_IMODE(recording.stat().st_mode) == 0o660
    key = b"test-only-backup-signature-key-32-bytes"
    path = data / "backup-operations/results/result.json"
    write_signed(path, {"status": "SUCCEEDED"}, key, "result")
    assert path.stat().st_gid == GID
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert read_signed(path, key, "result") == {"status": "SUCCEEDED"}
    assert os.geteuid() == 0, "Permission preparation is tested in the one-shot checks container"


def test_signed_result_uses_parent_group_without_setgid_on_windows_bind_mounts(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "results"
    directory.mkdir()
    os.chown(directory, UID, GID)
    directory.chmod(0o770)
    key = b"test-only-backup-signature-key-32-bytes"
    path = directory / "result.json"
    write_signed(path, {"status": "SUCCEEDED"}, key, "result")
    assert path.stat().st_uid == 0 and path.stat().st_gid == GID
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert read_signed(path, key, "result") == {"status": "SUCCEEDED"}
