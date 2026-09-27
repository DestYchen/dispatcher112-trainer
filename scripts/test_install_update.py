import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import install_update as install
import compose as installed_compose


@pytest.mark.parametrize(
    "change",
    [
        None,
        "archive",
        "key",
        "expired",
        "signature",
        "unexpected_archive",
        "restored_installation",
    ],
)
def test_signed_operator_request_cannot_change_package_or_trust(
    tmp_path, monkeypatch, change
):
    secrets = tmp_path / ".secrets"
    (secrets / "backup-control").mkdir(parents=True)
    key = b"operator-request-test-secret-32-bytes"
    (secrets / "backup-control/token").write_bytes(key)
    session_secret = "original-installation-session-secret-32-bytes"
    current_session = (
        "recovered-installation-session-secret-32-bytes"
        if change == "restored_installation"
        else session_secret
    )
    monkeypatch.setattr(
        install,
        "configuration",
        lambda root: {
            "services": {"backend": {"environment": {"JWT_SECRET": current_session}}}
        },
    )
    key = install.catalog.request_key(key, session_secret)
    public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    (secrets / "software-publisher.pub").write_bytes(public)
    archive = tmp_path / "release.zip"
    archive.write_bytes(
        b"fixture package; full archive verification is tested separately"
    )
    now = datetime.now(timezone.utc)
    identity = str(uuid4())
    value = {
        "schema": install.catalog.REQUEST_PURPOSE,
        "id": identity,
        "update_id": identity,
        "actor_id": str(uuid4()),
        "package_id": str(uuid4()),
        "action": "apply",
        "package_sha256": install.file_digest(archive),
        "publisher_sha256": hashlib.sha256(public).hexdigest(),
        "version": "1.2.3",
        "reason": "Operator acceptance",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }
    if change == "unexpected_archive":
        value.update(action="activate", package_id=None)
    envelope = install.catalog.signed_request(value, key)
    if change == "archive":
        archive.write_bytes(b"different package")
    elif change == "key":
        value["publisher_sha256"] = "0" * 64
        envelope = install.catalog.signed_request(value, key)
    elif change == "expired":
        envelope["value"]["created_at"] = (now - timedelta(days=2)).isoformat()
        envelope["value"]["expires_at"] = (now - timedelta(days=1)).isoformat()
        envelope["signature"] = install.catalog.signature(
            envelope["value"], key, install.catalog.REQUEST_PURPOSE
        )
    elif change == "signature":
        envelope["signature"] = "0" * 64
    path = tmp_path / "request.json"
    path.write_text(json.dumps(envelope))
    monkeypatch.setattr(
        install, "unpack_package", lambda path, public: {"version": "1.2.3"}
    )
    if change:
        with pytest.raises(ValueError):
            install.operator_request(tmp_path, path, archive)
    else:
        assert install.operator_request(tmp_path, path, archive) == value


@pytest.fixture
def operation(tmp_path, monkeypatch):
    stage = tmp_path / ".updates/release"
    stage.mkdir(parents=True)
    state = {
        "root": str(tmp_path),
        "project": "dispatcher_update_test",
        "id": str(uuid4()),
        "actor": str(uuid4()),
        "version": "1.2.3",
        "package_sha256": "a" * 64,
        "reason": "Update acceptance",
        "phase": "PREPARED",
        "database": "dispatcher",
        "owner": "dispatcher",
        "failed_database": "dispatcher_failed_0123456789ab",
    }
    events = []
    database_state = {"phase": "ABSENT"}
    monkeypatch.setattr(
        install, "no_oneoffs", lambda project: events.append("check-oneoffs")
    )
    monkeypatch.setattr(
        install,
        "save",
        lambda directory, value, key: events.append("save:" + value["phase"]),
    )

    def database(directory, value, action, *args):
        events.append("database:" + action)
        if action == "begin" and database_state["phase"] == "ABSENT":
            database_state["phase"] = "STARTED"
        if action == "export-audit":
            (directory / "audit.json").write_bytes(b"test export")
        if action == "merge-audit":
            database_state["phase"] = "ROLLING_BACK"
        return dict(database_state)

    def advance(directory, value, phase, details=None):
        events.append("advance:" + phase)
        database_state["phase"] = phase
        return dict(database_state)

    def composition(directory, value, which, *args, **kwargs):
        events.append(which + ":" + ("migrate" if args[-1] == "migrate" else args[0]))
        return ""

    def backup(directory, value, key):
        events.append("backup-verified")
        value.update(
            backup="backup-20260926T190000Z-0123456789ab", dump_sha256="b" * 64
        )

    monkeypatch.setattr(install, "database", database)
    monkeypatch.setattr(install, "advance", advance)
    monkeypatch.setattr(install, "composition", composition)
    monkeypatch.setattr(install, "backup", backup)
    monkeypatch.setattr(install, "sql", lambda *args, **kwargs: "migration_head")
    monkeypatch.setattr(
        install,
        "start",
        lambda d, s, which, **kwargs: events.append(which + ":healthy"),
    )
    monkeypatch.setattr(
        install, "restore_database", lambda *args: events.append("restore-database")
    )
    monkeypatch.setattr(
        install.files, "apply_files", lambda *args: events.append("apply-files")
    )
    monkeypatch.setattr(
        install.files, "rollback_files", lambda *args: events.append("rollback-files")
    )
    monkeypatch.setattr(
        install.files, "verify_tree", lambda *args: events.append("verify-files")
    )
    monkeypatch.setattr(install.files, "transaction", lambda *args: {"after": []})
    monkeypatch.setattr(
        install,
        "publish_deployment",
        lambda d, s, k, which: events.append("publish:" + which),
    )
    return stage, state, events, database_state


def test_installation_orders_quiescence_backup_files_migration_and_health(operation):
    stage, state, events, database_state = operation
    install.perform(stage, state, b"key", "apply")
    assert state["phase"] == database_state["phase"] == "READY"
    ordered = [
        "database:begin",
        "publish:old",
        "old:stop",
        "backup-verified",
        "advance:BACKED_UP",
        "apply-files",
        "new:migrate",
        "advance:MIGRATED",
        "new:healthy",
        "advance:READY",
    ]
    positions = [events.index(event) for event in ordered]
    assert positions == sorted(positions)
    assert "advance:ACTIVE" not in events
    assert events.index("save:PREPARED") < events.index("database:begin")


def test_retained_tags_keep_each_release_image_available(monkeypatch):
    tags = {}

    def run(*args):
        if args[2] == "tag":
            tags[args[-1]] = args[-2]
            return ""
        return tags[args[-1]]

    monkeypatch.setattr(install, "run", run)
    deployments = {
        which: {"services": {"backend": {"image": "sha256:" + digest * 64}}}
        for which, digest in (("old", "a"), ("new", "b"), ("helper", "c"))
    }
    result = install.retain_images(uuid4(), deployments)
    assert result == tags and len(tags) == 3
    assert set(tags.values()) == {"sha256:" + c * 64 for c in "abc"}


@pytest.mark.parametrize("failure", [None, "absent", "missing", "stopped", "unhealthy"])
def test_rollback_uses_actual_healthy_images_even_after_tags_changed(
    monkeypatch, failure
):
    containers = [
        {
            "Image": "sha256:" + "a" * 64,
            "Config": {"Labels": {"com.docker.compose.service": name}},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        for name in install.RUNTIME
    ]
    if failure == "missing":
        containers.pop()
    elif failure == "stopped":
        containers[0]["State"]["Running"] = False
    elif failure == "unhealthy":
        containers[0]["State"]["Health"]["Status"] = "unhealthy"
    monkeypatch.setattr(
        install.recovery,
        "installed_images",
        lambda previous: dict.fromkeys(install.SERVICES, "sha256:" + "b" * 64),
    )
    monkeypatch.setattr(
        install,
        "run",
        lambda *args: (
            ("" if failure == "absent" else "container-id")
            if args[1] == "ps"
            else json.dumps(containers)
        ),
    )
    if failure:
        with pytest.raises(ValueError):
            install.source_images({"name": "installation"})
    else:
        assert set(install.source_images({"name": "installation"}).values()) == {
            "sha256:" + "a" * 64
        }


def test_publication_persists_exact_deployment_and_supports_restart(tmp_path):
    root = tmp_path.resolve()
    stage = root / ".updates" / ("install-" + uuid4().hex)
    stage.mkdir(parents=True)
    key = b"k" * 32
    (root / ".secrets").mkdir()
    (root / ".secrets/backup-signing.key").write_bytes(key)
    external = root / "recovery-compose.json"
    external.write_text('{"name":"original"}')
    state = {
        "root": str(root),
        "project": "test",
        "id": str(uuid4()),
        "compose_hashes": {},
        "deployment": str(external),
        "deployment_original_sha256": install.file_digest(external),
    }
    for which in ("new", "old"):
        path = stage / f"{which}-compose.json"
        path.write_text(json.dumps({"name": which}))
        state["compose_hashes"][which] = install.file_digest(path)
    for which in ("new", "new", "old", "old"):
        install.publish_deployment(stage, state, key, which)
        path = stage / f"{which}-compose.json"
        assert external.read_bytes() == path.read_bytes()
        assert installed_compose.deployment(root) == (path, "test")
        command = installed_compose.command(root, ["up", "--build", "--wait"])
        assert command[-3:] == ["up", "--build", "--wait"]
        assert command[command.index("-f") + 1] == str(path)
    external.write_text('{"name":"another-operator"}')
    with pytest.raises(ValueError, match="another operator"):
        install.publish_deployment(stage, state, key, "new")
    assert installed_compose.deployment(root)[0].name == "old-compose.json"
    (stage / "old-compose.json").write_text('{"name":"corrupt"}')
    with pytest.raises(ValueError, match="changed"):
        installed_compose.command(root, ["up"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["-f", "another.json"],
        ["--file=another.json"],
        ["--project-name=another"],
        ["-p", "another"],
        ["--project-directory", "elsewhere"],
    ],
)
def test_installed_commands_cannot_override_pinned_project(
    tmp_path, monkeypatch, arguments
):
    monkeypatch.setattr(
        installed_compose, "deployment", lambda root: (root / "compose.json", "project")
    )
    with pytest.raises(ValueError, match="without project or file overrides"):
        installed_compose.command(tmp_path, arguments)


def test_development_compose_command_is_unchanged_without_active_release(tmp_path):
    assert installed_compose.command(tmp_path, ["config", "--quiet"]) == [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "config",
        "--quiet",
    ]


@pytest.mark.parametrize("tls", [False, True])
def test_persistent_limits_preserve_the_selected_transport_mode(
    tmp_path, monkeypatch, tls
):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.delenv("COMPOSE_PATH_SEPARATOR", raising=False)
    if tls:
        (tmp_path / ".env").write_text(
            "COMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE=docker-compose.yml;docker-compose.tls.yml\n"
        )
    installed_compose.save_resource_limits(tmp_path, "backend", 1, 512)
    command = installed_compose.command(tmp_path, ["up", "--detach"])
    assert (str(tmp_path / "docker-compose.tls.yml") in command) is tls
    assert str(tmp_path / ".runtime-resources.compose.json") in command


@pytest.mark.parametrize("fail_at", ["backup", "files", "migration", "health"])
def test_installation_failure_runs_rollback_before_reporting_failure(
    operation, monkeypatch, fail_at
):
    stage, state, events, _ = operation

    def fail(*args, **kwargs):
        raise RuntimeError("injected installation failure")

    if fail_at == "backup":
        monkeypatch.setattr(install, "backup", fail)
    elif fail_at == "files":
        monkeypatch.setattr(install.files, "apply_files", fail)
    elif fail_at == "migration":
        original = install.composition

        def composition(d, s, which, *args, **kwargs):
            if which == "new" and args[-1] == "migrate":
                fail()
            return original(d, s, which, *args, **kwargs)

        monkeypatch.setattr(install, "composition", composition)
    else:
        monkeypatch.setattr(
            install,
            "start",
            lambda d, s, which, **kwargs: (
                fail() if which == "new" else events.append("old:healthy")
            ),
        )
    with pytest.raises(RuntimeError, match="injected"):
        install.perform(stage, state, b"key", "apply")
    assert state["phase"] == "ROLLED_BACK"
    assert (
        events.index("rollback-files")
        < events.index("old:healthy")
        < events.index("advance:ROLLED_BACK")
    )
    if fail_at != "backup":
        assert (
            events.index("database:export-audit")
            < events.index("restore-database")
            < events.index("database:merge-audit")
        )


def test_busy_database_never_stops_services_or_rolls_back_another_update(
    operation, monkeypatch
):
    stage, state, events, _ = operation

    def busy(*args, **kwargs):
        raise RuntimeError("busy database")

    monkeypatch.setattr(install, "database", busy)
    with pytest.raises(RuntimeError, match="busy"):
        install.perform(stage, state, b"key", "apply")
    assert "old:stop" not in events and "rollback-files" not in events


@pytest.mark.parametrize("phase", ["ACTIVE", "ACTIVATING"])
def test_unconditional_rollback_cannot_remove_post_activation_data(operation, phase):
    stage, state, events, database_state = operation
    state["phase"] = phase
    database_state["phase"] = "ACTIVE"
    with pytest.raises(ValueError):
        install.rollback(stage, state, b"key")
    assert events == []


def test_stale_host_journal_cannot_override_database_activation(operation):
    stage, state, events, database_state = operation
    state.update(phase="READY", begun=True)
    database_state["phase"] = "ACTIVE"
    with pytest.raises(ValueError, match="already activated"):
        install.rollback(stage, state, b"key")
    assert "rollback-files" not in events and "new:stop" not in events


def test_resume_after_database_activation_only_finishes_host_journal(operation):
    stage, state, events, database_state = operation
    state.update(phase="READY", begun=True)
    database_state["phase"] = "ACTIVE"
    install.apply(stage, state, b"key")
    assert state["phase"] == "ACTIVE"
    assert "old:stop" not in events and "apply-files" not in events


def test_activation_intent_is_durable_before_database_release(operation):
    stage, state, events, database_state = operation
    state["phase"], database_state["phase"] = "READY", "READY"
    install.activate(stage, state, b"key")
    assert (
        events.index("new:healthy")
        < events.index("save:ACTIVATING")
        < events.index("publish:new")
        < events.index("advance:ACTIVE")
    )
    assert state["phase"] == "ACTIVE"
    before = events.copy()
    install.activate(stage, state, b"key")
    assert events == before


def test_activation_can_finish_after_process_dies_before_local_commit(operation):
    stage, state, _, database_state = operation
    state["phase"], database_state["phase"] = "ACTIVATING", "ACTIVE"
    assert install.activate(stage, state, b"key")["phase"] == "ACTIVE"


@pytest.mark.parametrize("db_phase", ["ABSENT", "ROLLED_BACK"])
def test_rollback_of_never_started_or_completed_update_does_not_touch_program(
    operation, db_phase
):
    stage, state, events, database_state = operation
    state["begin_intent"] = True
    database_state["phase"] = db_phase
    install.rollback(stage, state, b"key")
    assert state["phase"] == "ROLLED_BACK"
    assert "rollback-files" not in events and "new:stop" not in events


def test_rollback_recovers_start_committed_before_host_journal(operation):
    stage, state, events, database_state = operation
    state["begin_intent"] = True
    database_state["phase"] = "STARTED"
    install.rollback(stage, state, b"key")
    assert "advance:ROLLING_BACK" in events and "advance:ROLLED_BACK" in events


def test_rollback_resumes_after_database_rename_without_querying_missing_database(
    operation, monkeypatch
):
    stage, state, events, _ = operation
    state.update(phase="ROLLING_BACK", begun=True, dump_sha256="b" * 64)
    (stage / "audit.json").write_bytes(b"test signed checkpoint")
    monkeypatch.setattr(
        install.files,
        "read_record",
        lambda *args, **kwargs: {
            "update": {"id": state["id"], "package_sha256": state["package_sha256"]}
        },
    )
    original = install.database

    def database(d, s, action, *args):
        if action == "status":
            raise AssertionError("The renamed database is not the current database")
        return original(d, s, action, *args)

    monkeypatch.setattr(install, "database", database)
    install.rollback(stage, state, b"key")
    assert state["phase"] == "ROLLED_BACK"
    assert "restore-database" in events and "database:merge-audit" in events


def test_foreign_audit_checkpoint_is_refused_before_any_restart(operation, monkeypatch):
    stage, state, events, _ = operation
    state.update(phase="ROLLING_BACK", begun=True)
    (stage / "audit.json").write_bytes(b"other")
    monkeypatch.setattr(
        install.files,
        "read_record",
        lambda *args, **kwargs: {"update": {"id": str(uuid4())}},
    )
    with pytest.raises(ValueError, match="another update"):
        install.rollback(stage, state, b"key")
    assert "new:stop" not in events


def test_live_oneoff_prevents_any_repeat(operation, monkeypatch):
    stage, state, events, _ = operation

    def active(project):
        raise ValueError("one-shot process running")

    monkeypatch.setattr(install, "no_oneoffs", active)
    with pytest.raises(ValueError, match="running"):
        install.apply(stage, state, b"key")
    assert events == []


def test_saved_configuration_is_checked_before_docker_execution(tmp_path, monkeypatch):
    path = tmp_path / "old-compose.json"
    path.write_bytes(b"original")
    state = {"compose_hashes": {"old": hashlib.sha256(b"original").hexdigest()}}
    path.write_bytes(b"different")
    monkeypatch.setattr(
        install, "run", lambda *args, **kwargs: pytest.fail("Docker must not run")
    )
    with pytest.raises(ValueError, match="changed"):
        install.composition(tmp_path, state, "old", "up")


def test_process_lock_prevents_two_installers_and_releases_after_failure(tmp_path):
    with install.installation_lock(tmp_path):
        with pytest.raises(OSError):
            with install.installation_lock(tmp_path):
                pytest.fail("Concurrent installers acquired the same lock")
    with install.installation_lock(tmp_path):
        assert (tmp_path / ".updates/installer.lock").exists()


def test_restore_refuses_overwriting_unmarked_database(tmp_path, monkeypatch):
    dump = tmp_path / "before.dump"
    dump.write_bytes(b"verified-dump")
    state = {
        "database": "dispatcher",
        "failed_database": "dispatcher_failed_0123456789ab",
        "id": str(uuid4()),
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(
        install,
        "sql",
        lambda d, s, statement, **kwargs: (
            "dispatcher\ndispatcher_failed_0123456789ab"
            if "SELECT datname" in statement
            else "unrelated-database"
        ),
    )
    monkeypatch.setattr(
        install,
        "composition",
        lambda *args, **kwargs: pytest.fail("Do not drop an unrelated database"),
    )
    with pytest.raises(ValueError, match="verified partial restore"):
        install.restore_database(tmp_path, state, b"key")
