import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import recover_snapshot as recovery


@pytest.mark.parametrize("copies", [1, 2])
def test_disk_reserve_covers_data_and_docker_host_before_any_write(
    monkeypatch, copies, tmp_path
):
    monkeypatch.setattr(recovery, "ROOT", tmp_path)
    monkeypatch.setattr(recovery, "docker_storage_path", lambda: tmp_path)
    required = copies * 1000 + 200 + recovery.RECOVERY_DISK_RESERVE
    monkeypatch.setattr(
        recovery.shutil, "disk_usage", lambda path: SimpleNamespace(free=required - 1)
    )
    with pytest.raises(ValueError, match="free host bytes"):
        recovery.require_disk_space(1000, copies, host_bytes=200)
    monkeypatch.setattr(
        recovery.shutil, "disk_usage", lambda path: SimpleNamespace(free=required)
    )
    recovery.require_disk_space(1000, copies, host_bytes=200)


@pytest.mark.parametrize("full_disk", ["workspace", "docker", None])
def test_separate_workspace_and_docker_disks_are_both_checked(
    monkeypatch, tmp_path, full_disk
):
    workspace = tmp_path / "workspace"
    storage = tmp_path / "docker"
    monkeypatch.setattr(recovery, "ROOT", workspace)
    monkeypatch.setattr(recovery, "docker_storage_path", lambda: storage)
    original_stat = Path.stat

    def disk_stat(path, *args, **kwargs):
        if path in (workspace, storage):
            return SimpleNamespace(st_dev=1 if path == workspace else 2)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disk_stat)
    required = {
        workspace: 200 + recovery.RECOVERY_DISK_RESERVE,
        storage: 2000 + recovery.RECOVERY_DISK_RESERVE,
    }
    monkeypatch.setattr(
        recovery.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=required[path] - (path.name == full_disk)),
    )
    if full_disk:
        with pytest.raises(ValueError, match=full_disk):
            recovery.require_disk_space(1000, 2, host_bytes=200)
    else:
        recovery.require_disk_space(1000, 2, host_bytes=200)


@pytest.mark.parametrize("custom", [True, False])
def test_windows_storage_uses_desktop_setting_or_default(monkeypatch, tmp_path, custom):
    monkeypatch.setattr(recovery.sys, "platform", "win32")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/dockerDesktopLinuxEngine")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(
        recovery, "run", lambda *args: "linux|Docker Desktop|/var/lib/docker"
    )
    settings = tmp_path / "roaming/Docker/settings-store.json"
    settings.parent.mkdir(parents=True)
    storage = tmp_path / ("other-drive/wsl" if custom else "local/Docker/wsl")
    storage.mkdir(parents=True)
    settings.write_text(
        json.dumps({"CustomWslDistroDir": str(storage)} if custom else {})
    )
    assert recovery.docker_storage_path() == storage
    settings.write_text(json.dumps({"WslEngineEnabled": False}))
    with pytest.raises(ValueError, match="WSL2"):
        recovery.docker_storage_path()


def test_native_linux_storage_and_context_override(monkeypatch, tmp_path):
    monkeypatch.setattr(recovery.sys, "platform", "linux")
    monkeypatch.setenv("DOCKER_CONTEXT", "local-test")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
    calls = []

    def run(*args):
        calls.append(args)
        return (
            json.dumps("unix:///var/run/docker.sock")
            if args[1] == "context"
            else f"linux|Ubuntu|{tmp_path}"
        )

    monkeypatch.setattr(recovery, "run", run)
    assert recovery.docker_storage_path() == tmp_path
    assert "local-test" in calls[0]
    monkeypatch.delenv("DOCKER_CONTEXT")
    with pytest.raises(ValueError, match="local Docker"):
        recovery.docker_storage_path()
    assert len(calls) == 2


def test_unavailable_storage_is_not_replaced_with_workspace_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(recovery.sys, "platform", "linux")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setattr(
        recovery, "run", lambda *args: f"linux|Ubuntu|{tmp_path / 'missing'}"
    )
    with pytest.raises(ValueError, match="unavailable"):
        recovery.docker_storage_path()


@pytest.mark.parametrize("relative", ["../outside", "other", ".recovery"])
def test_target_stays_in_own_private_directory(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(recovery, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        recovery.checked_target(tmp_path / relative)
    assert (
        recovery.checked_target(tmp_path / ".recovery/run")
        == tmp_path / ".recovery/run"
    )


def test_existing_target_is_never_overwritten(tmp_path):
    (tmp_path / "keep").write_bytes(b"existing data")
    with pytest.raises(ValueError, match="exists"):
        recovery.prepare(SimpleNamespace(), tmp_path)
    assert (tmp_path / "keep").read_bytes() == b"existing data"


def archive_command(tmp_path, name, kind=tarfile.REGTYPE):
    archive = tmp_path / "archive.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind == tarfile.REGTYPE:
            member.size = 7
            output.addfile(member, io.BytesIO(b"content"))
        else:
            member.linkname = "../../private"
            output.addfile(member)
    return [
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
        str(archive),
    ]


def test_streaming_export_copies_only_regular_files(tmp_path):
    target = tmp_path / "workspace"
    recovery.extract_host(archive_command(tmp_path, "backend/app/main.py"), target)
    assert (target / "backend/app/main.py").read_bytes() == b"content"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("C:/private", tarfile.REGTYPE),
        ("bad\\path", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("link", tarfile.LNKTYPE),
    ],
)
def test_streaming_export_rejects_traversal_and_links(tmp_path, name, kind):
    with pytest.raises(ValueError):
        recovery.extract_host(
            archive_command(tmp_path, name, kind), tmp_path / "workspace"
        )
    assert not (tmp_path / "escape").exists()


def test_command_failure_does_not_expose_its_private_output():
    with pytest.raises(RuntimeError) as caught:
        recovery.run(
            sys.executable, "-c", "import sys; print('private-value'); sys.exit(1)"
        )
    assert "private-value" not in str(caught.value)


def test_dependency_mismatch_rejects_an_installed_image(tmp_path, monkeypatch):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/requirements.lock").write_text("exact version")
    monkeypatch.setattr(recovery, "run", lambda *args: "wrong-image-digest")
    with pytest.raises(ValueError, match="lock file"):
        recovery.check_dependencies({"backend": "sha256:test"}, tmp_path)


@pytest.mark.parametrize("schedule_restored", [True, False])
def test_activation_restores_schedule_only_once(
    monkeypatch, tmp_path, schedule_restored
):
    target = tmp_path / ".recovery/test"
    target.mkdir(parents=True)
    state = {
        "project": "dispatcher_recovery_123456abcdef",
        "status": "ACTIVE" if schedule_restored else "PREPARED",
        "snapshot": "test-snapshot",
        "backup_schedule_restored": schedule_restored,
    }
    recovery.save(target / "state.json", state)
    recovery.save(target / "extraction.json", {"original_schedule": {"enabled": True}})
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/backup-signing.key").write_bytes(b"key")
    monkeypatch.setattr(recovery, "ROOT", tmp_path)
    monkeypatch.setattr(
        recovery.sys,
        "argv",
        ["recover_snapshot.py", "activate", "--target", str(target)],
    )
    monkeypatch.setattr(
        recovery, "read_manifest", lambda *args: {"files": [{"size": 1000}]}
    )
    calls = []
    monkeypatch.setattr(
        recovery, "require_disk_space", lambda *args: calls.append("space")
    )
    monkeypatch.setattr(
        recovery, "compose", lambda target, *args, **kwargs: calls.append(args[0])
    )

    def activate(*args):
        calls.append("database")
        return {"status": "ACTIVE"}

    monkeypatch.setattr(recovery, "database", activate)
    recovery.main()
    saved = json.loads((target / "state.json").read_text())
    assert saved["status"] == "ACTIVE" and saved["backup_schedule_restored"]
    assert calls == (
        ["up", "database"] if schedule_restored else ["space", "up", "database", "exec"]
    )


def test_activation_disk_failure_precedes_all_service_and_database_changes(
    monkeypatch, tmp_path
):
    target = tmp_path / ".recovery/test"
    target.mkdir(parents=True)
    recovery.save(
        target / "state.json", {"status": "PREPARED", "snapshot": "test-snapshot"}
    )
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/backup-signing.key").write_bytes(b"key")
    monkeypatch.setattr(recovery, "ROOT", tmp_path)
    monkeypatch.setattr(
        recovery.sys,
        "argv",
        ["recover_snapshot.py", "activate", "--target", str(target)],
    )
    monkeypatch.setattr(
        recovery, "read_manifest", lambda *args: {"files": [{"size": 1000}]}
    )
    calls = []
    monkeypatch.setattr(recovery, "compose", lambda *args: calls.append("compose"))
    monkeypatch.setattr(recovery, "database", lambda *args: calls.append("database"))

    def full_disk(*args):
        raise ValueError("No free host bytes")

    monkeypatch.setattr(recovery, "require_disk_space", full_disk)
    with pytest.raises(ValueError, match="free host bytes"):
        recovery.main()
    assert calls == []
    assert json.loads((target / "state.json").read_text())["status"] == "PREPARED"
