import json
from types import SimpleNamespace

import pytest

import software_package as packages
from app.operations import update_package
from app.operations.recovery_compose import SERVICES
from tests.test_program_snapshots import program_fixture
from tests.test_update_package import signing_keys


@pytest.fixture
def release(tmp_path, monkeypatch):
    source = program_fixture(tmp_path / "source")
    (source / "infra").mkdir()
    (source / "infra/template.conf").write_bytes(b"template")
    for name in update_package.DEPLOYMENT_FILES:
        (source / name).write_bytes(b"services: {}")
    private, public = signing_keys()
    archive = tmp_path / "release.zip"
    manifest = update_package.create_package(
        source,
        archive,
        private,
        "1.2.3",
        "Local test release",
        dict.fromkeys(SERVICES, "sha256:" + "a" * 64),
    )
    monkeypatch.setattr(packages, "ROOT", source)
    return SimpleNamespace(
        source=source, archive=archive, public=public, manifest=manifest
    )


def test_preparation_checks_local_images_and_lockfiles_without_changing_installation(
    release, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        packages.recovery, "run", lambda *args: calls.append(args) or args[-1]
    )
    dependencies = []
    monkeypatch.setattr(
        packages.recovery,
        "check_dependencies",
        lambda images, workspace: dependencies.append((images, workspace.is_dir())),
    )
    before = {p: p.read_bytes() for p in release.source.rglob("*") if p.is_file()}
    target = release.source / ".updates/test"
    result = packages.prepare(release.archive, release.public, target)
    assert result["phase"] == "VERIFIED"
    assert (target / "release.zip").read_bytes() == release.archive.read_bytes()
    assert result["package_sha256"] == packages.file_digest(target / "release.zip")
    assert dependencies == [(release.manifest["images"], True)]
    assert calls == [
        ("docker", "image", "inspect", "--format", "{{.Id}}", "sha256:" + "a" * 64)
    ]
    assert (
        json.loads((target / "state.json").read_text())["manifest"] == release.manifest
    )
    assert packages.integrity(release.archive, release.public, target / "program")[
        "passed"
    ]
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize(
    "failure", ["missing_image", "wrong_lockfile", "disk_full", "signature"]
)
def test_preparation_failure_keeps_installation_and_publishes_no_target(
    release, monkeypatch, failure
):
    calls = []

    def inspect(*args):
        calls.append(args)
        if failure == "missing_image":
            raise RuntimeError("missing image")
        return args[-1]

    def dependencies(images, workspace):
        if failure == "wrong_lockfile":
            raise ValueError("lock mismatch")

    monkeypatch.setattr(packages.recovery, "run", inspect)
    monkeypatch.setattr(packages.recovery, "check_dependencies", dependencies)
    if failure == "disk_full":
        monkeypatch.setattr(
            packages.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)
        )
    public = signing_keys()[1] if failure == "signature" else release.public
    before = {p: p.read_bytes() for p in release.source.rglob("*") if p.is_file()}
    target = release.source / ".updates/test"
    with pytest.raises((ValueError, RuntimeError)):
        packages.prepare(release.archive, public, target)
    assert not target.exists()
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not any(p.is_file() for p in (release.source / ".updates").rglob("*"))
    if failure in ("disk_full", "signature"):
        assert calls == []


@pytest.mark.parametrize("name", [".", ".updates", ".updates/../data", "../outside"])
def test_staging_target_cannot_be_installation_or_data(release, name):
    with pytest.raises(ValueError, match="below .updates"):
        packages.checked_target(release.source / name)


def test_existing_stage_is_preserved(release):
    target = release.source / ".updates/old"
    target.mkdir(parents=True)
    (target / "sentinel").write_bytes(b"preserve")
    with pytest.raises(ValueError, match="already exists"):
        packages.prepare(release.archive, release.public, target)
    assert (target / "sentinel").read_bytes() == b"preserve"


@pytest.mark.parametrize("kind", ["intact", "changed", "missing", "extra", "private"])
def test_integrity_reports_program_changes_without_touching_data(release, kind):
    main = release.source / "backend/app/main.py"
    if kind == "changed":
        main.write_bytes(b"corrupted application")
    elif kind == "missing":
        main.unlink()
    elif kind == "extra":
        (release.source / "backend/app/extra.py").write_bytes(b"unexpected code")
    elif kind == "private":
        (release.source / "infra/private.key").write_bytes(b"not-a-release-file")
    (release.source / ".env").write_bytes(b"installation secret is outside the program")
    result = packages.integrity(release.archive, release.public, release.source)
    assert result["passed"] == (kind == "intact")
    if kind in ("changed", "missing"):
        assert result[kind] == ["backend/app/main.py"]
    if kind == "extra":
        assert result["extra"] == ["backend/app/extra.py"]
    if kind == "private":
        assert result["invalid_inventory"]


def test_signing_keys_are_generated_once_and_no_private_bytes_returned(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(packages.sys, "platform", "linux")
    target = tmp_path / "publisher"
    result = packages.create_signer(target)
    assert set(result) == {"public_key_sha256"}
    original = (target / "publisher.key").read_bytes()
    assert b"PRIVATE KEY" in original
    assert b"PUBLIC KEY" in (target / "publisher.pub").read_bytes()
    with pytest.raises(FileExistsError):
        packages.create_signer(target)
    assert (target / "publisher.key").read_bytes() == original


def test_windows_signing_acl_failure_happens_before_key_write(tmp_path, monkeypatch):
    monkeypatch.setattr(packages.sys, "platform", "win32")
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "whoami":
            return '"user","S-1-5-21-123"'
        raise RuntimeError("ACL denied")

    monkeypatch.setattr(packages, "windows_command", run)
    directory = tmp_path / "publisher"
    with pytest.raises(RuntimeError, match="ACL denied"):
        packages.create_signer(directory)
    assert list(directory.iterdir()) == []
    assert calls[1] == (
        "icacls",
        str(directory),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-21-123:(OI)(CI)F",
    )


def test_image_inventory_contains_only_known_ids_and_never_prints_compose_secrets(
    monkeypatch, capsys
):
    expected = dict.fromkeys(SERVICES, "sha256:" + "a" * 64)
    source = {"private": "do not print", "services": {}}
    monkeypatch.setattr(packages.recovery, "run", lambda *args: json.dumps(source))
    monkeypatch.setattr(
        packages.recovery, "installed_images", lambda value: expected.copy()
    )
    assert packages.inventory() == expected
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(
        packages.recovery, "installed_images", lambda value: {"unknown": "tag"}
    )
    with pytest.raises(ValueError, match="composition"):
        packages.inventory()


def test_dependency_probes_never_pull_images_or_connect_to_network(
    release, monkeypatch
):
    calls = []

    def run(*args):
        calls.append(args)
        path = (
            "backend/requirements.lock"
            if "python" in args
            else "frontend/package-lock.json"
        )
        return packages.file_digest(release.source / path)

    monkeypatch.setattr(packages.recovery, "run", run)
    packages.recovery.check_dependencies(release.manifest["images"], release.source)
    assert len(calls) == 7
    for call in calls:
        assert call[call.index("--pull") + 1] == "never"
        assert call[call.index("--network") + 1] == "none"
        assert "--read-only" in call
        assert call[call.index("--cap-drop") + 1] == "ALL"


def test_cli_integrity_exit_code_reflects_detected_corruption(
    release, monkeypatch, capsys
):
    key = release.source.parent / "publisher.pub"
    key.write_bytes(release.public)
    monkeypatch.setattr(
        packages.sys,
        "argv",
        [
            "software_package.py",
            "integrity",
            "--package",
            str(release.archive),
            "--trust-key",
            str(key),
            "--root",
            str(release.source),
        ],
    )
    assert packages.main() == 0
    assert json.loads(capsys.readouterr().out)["passed"]
    (release.source / "backend/app/main.py").unlink()
    assert packages.main() == 1
    assert not json.loads(capsys.readouterr().out)["passed"]
