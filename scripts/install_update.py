"""Install a signed local release under maintenance; preserve the failed database on rollback."""

import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import recover_snapshot as recovery
import compose as installed_compose

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.operations import update_files as files  # noqa: E402
from app.operations import update_catalog as catalog  # noqa: E402
from app.operations.program_files import program_files  # noqa: E402
from app.operations.recovery_compose import PROJECT, SERVICES, recovery_compose  # noqa: E402
from app.operations.snapshot import SNAPSHOT_NAME, file_digest  # noqa: E402
from app.operations.update_deployment import pin_deployment, update_deployment  # noqa: E402
from app.operations.update_package import unpack_package  # noqa: E402

PURPOSE = "software-installation-1"
STATE = "installation.json"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
RUNTIME = sorted(SERVICES - {"prepare_runtime", "migrate", "checks", "frontend_checks"})
WRITERS = sorted(set(RUNTIME) - {"postgres", "redis"})


def run(*args, cwd=ROOT, incoming=None, output=None):
    with contextlib.ExitStack() as opened:
        options = {"input": incoming}
        if isinstance(incoming, Path):
            options = {"stdin": opened.enter_context(incoming.open("rb"))}
        result = subprocess.run(
            args,
            cwd=cwd,
            stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
    if result.returncode:
        raise RuntimeError(
            f"{args[0]} failed ({result.returncode}); recovery files were preserved"
        )
    return result.stdout.decode("utf-8").strip() if output is None else ""


def configuration(root, deployment=None, *, program=None):
    if deployment is None and program is None:
        selected = installed_compose.deployment(root)
        if selected:
            deployment = selected[0]
    paths = (
        [deployment]
        if deployment
        else [
            (program or root) / "docker-compose.yml",
            (program or root) / "docker-compose.tls.yml",
        ]
    )
    overlay = installed_compose.resource_overlay(root)
    if overlay is not None:
        paths.append(overlay)
    args = [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "--env-file",
        str(root / ".env"),
        "--profile",
        "*",
    ]
    for path in paths:
        args += ["-f", str(path)]
    return json.loads(run(*args, "config", "--format", "json", cwd=root))


@contextlib.contextmanager
def installation_lock(root):
    directory = root / ".updates"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("Update directory must not be a link")
    path = directory / "installer.lock"
    if path.is_symlink():
        raise ValueError("Update lock must not be a link")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save(stage, state, key):
    files.write_record(stage / STATE, state, key, PURPOSE)


def composition(stage, state, which, *args, incoming=None, output=None):
    path = stage / f"{which}-compose.json"
    expected = state["compose_hashes"][which]
    if path.is_symlink() or file_digest(path) != expected:
        raise ValueError("The saved deployment configuration has changed")
    return run(
        "docker",
        "compose",
        "--project-directory",
        state["root"],
        "-p",
        state["project"],
        "-f",
        str(path),
        *args,
        cwd=state["root"],
        incoming=incoming,
        output=output,
    )


def database(stage, state, action, *args):
    files.verify_files(stage / "helper", state["helper_files"])
    result = composition(
        stage,
        state,
        "helper",
        "run",
        "--rm",
        "--no-deps",
        "--workdir",
        "/installer",
        "--volume",
        f"{stage / 'helper/backend'}:/installer:ro",
        "--volume",
        f"{stage}:/update",
        "--volume",
        f"{Path(state['root']) / '.secrets/backup-signing.key'}:/signing-key:ro",
        "--entrypoint",
        "python",
        "migrate",
        "-m",
        "app.operations.update_database",
        action,
        "--id",
        state["id"],
        "--actor",
        state["actor"],
        *args,
    )
    return json.loads(result.splitlines()[-1])


def advance(stage, state, phase, details=None):
    return database(
        stage,
        state,
        "advance",
        "--phase",
        phase,
        "--details",
        json.dumps(details or {}),
    )


def sql(stage, state, statement, *, which="old", database_name=None):
    return composition(
        stage,
        state,
        which,
        "exec",
        "-T",
        "postgres",
        "psql",
        "--username",
        state["owner"],
        "--dbname",
        database_name or state["database"],
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "--command",
        statement,
    )


def no_oneoffs(project):
    active = run(
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "label=com.docker.compose.oneoff=True",
        "--format",
        "{{.ID}}",
    )
    if active:
        raise ValueError(
            "A one-shot project process is still running; wait for its recorded result"
        )


def start(stage, state, which, *, force=False):
    args = [
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "240",
    ]
    if force:
        args.append("--force-recreate")
    composition(stage, state, which, *args, *RUNTIME)


def retain_images(identity, deployments):
    tags = {}
    for which, value in deployments.items():
        for name, service in value["services"].items():
            image = service["image"]
            tag = f"dispatcher112-update:{identity.hex}-{which}-{name}"
            run("docker", "image", "tag", image, tag)
            if run("docker", "image", "inspect", "--format", "{{.Id}}", tag) != image:
                raise ValueError("An image could not be retained for rollback")
            tags[tag] = image
    return tags


def source_images(previous):
    images = recovery.installed_images(previous)
    identities = run(
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={previous['name']}",
        "--filter",
        "label=com.docker.compose.oneoff=False",
        "--format",
        "{{.ID}}",
    ).splitlines()
    if not identities:
        raise ValueError("Start the selected installation before updating it")
    containers = json.loads(run("docker", "container", "inspect", *identities))
    runtime = {
        item["Config"]["Labels"].get("com.docker.compose.service"): item
        for item in containers
    }
    if not set(RUNTIME).issubset(runtime):
        raise ValueError("Start every installed runtime service before updating it")
    for name in RUNTIME:
        item = runtime[name]
        if (
            not item["State"]["Running"]
            or item["State"].get("Health", {}).get("Status", "healthy") != "healthy"
        ):
            raise ValueError("Repair the installed service health before updating it")
        images[name] = item["Image"]
    # These one-shot services use the same Dockerfiles/lockfiles as their runtime counterparts.
    for name in ("migrate", "checks", "prepare_runtime"):
        images[name] = images["backend"]
    images["frontend_checks"] = images["frontend"]
    return images


def publish_deployment(stage, state, key, which):
    source = stage / f"{which}-compose.json"
    digest = state["compose_hashes"][which]
    if source.is_symlink() or file_digest(source) != digest:
        raise ValueError("The saved deployment configuration has changed")
    external = state.get("deployment")
    if external:
        target = Path(external)
        allowed = {
            state["deployment_original_sha256"],
            *state["compose_hashes"].values(),
        }
        if target.resolve() != target or file_digest(target) not in allowed:
            raise ValueError("The selected deployment was changed by another operator")
        # Exact bytes preserve the verified digest across publication and retries.
        temporary = target.with_name(target.name + "." + state["id"] + ".partial")
        if temporary.is_symlink():
            raise ValueError("Deployment publication cannot use links")
        with temporary.open("wb") as output:
            output.write(source.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)
    files.write_record(
        Path(state["root"]) / ".updates/active.json",
        {
            "root": state["root"],
            "project": state["project"],
            "id": state["id"],
            "compose": f"{stage.name}/{which}-compose.json",
            "sha256": digest,
        },
        key,
        installed_compose.PURPOSE,
    )


def initial_state(
    root, archive, public_key, actor, reason, deployment, *, identity=None
):
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Select the resolved installation directory")
    key = (root / ".secrets/backup-signing.key").read_bytes().strip()
    if len(key) < 32 or not 5 <= len(reason.strip()) <= 1000:
        raise ValueError("An installation key and an update reason are required")
    recovery.docker_storage_path()
    manifest = unpack_package(archive, public_key)
    if (
        shutil.disk_usage(root).free
        < 3 * sum(item["size"] for item in manifest["files"]) + 256 * 1024 * 1024
    ):
        raise ValueError("Insufficient space for staging and rollback files")
    previous = configuration(root, deployment)
    if deployment and (deployment.resolve() != deployment or deployment.is_symlink()):
        raise ValueError("Select a resolved deployment configuration")
    no_oneoffs(previous["name"])
    if not run(
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={previous['name']}",
        "--filter",
        "label=com.docker.compose.service=postgres",
        "--format",
        "{{.ID}}",
    ):
        raise ValueError("Start the selected installation before updating it")
    images = source_images(previous)
    recovery.check_dependencies(images, root)
    for image in sorted(set(manifest["images"].values())):
        if run("docker", "image", "inspect", "--format", "{{.Id}}", image) != image:
            raise ValueError("A signed release image is not installed locally")
    identity = identity or uuid4()
    stage = root / ".updates" / ("install-" + identity.hex)
    stage.mkdir(mode=0o700)
    shutil.copyfile(archive, stage / "release.zip")
    if unpack_package(stage / "release.zip", public_key, stage / "program") != manifest:
        raise ValueError("Release changed during preparation")
    recovery.check_dependencies(manifest["images"], stage / "program")
    candidate = configuration(root, program=stage / "program")
    if PROJECT.fullmatch(previous["name"]):
        ports = previous["services"]["gateway"]["ports"]
        ui_port = next(int(p["published"]) for p in ports if p["target"] == 5173)
        api_port = next(int(p["published"]) for p in ports if p["target"] == 8000)
        media = previous["services"]["telephony"]["environment"]["SIP_MEDIA_ADDRESS"]
        candidate = recovery_compose(
            candidate,
            root,
            previous["name"],
            manifest["images"],
            ui_port,
            api_port,
            media,
        )
    candidate = pin_deployment(candidate, manifest["images"])
    recovery.save(stage / "candidate.json", candidate)
    candidate = configuration(root, stage / "candidate.json")
    candidate = update_deployment(previous, candidate, manifest["images"], root)
    old = pin_deployment(previous, images)
    # Major PostgreSQL upgrades require a separate cluster migration, not a file replacement.
    versions = []
    for image in (images["postgres"], manifest["images"]["postgres"]):
        version = run(
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "postgres",
            image,
            "--version",
        )
        match = re.search(r"PostgreSQL\) (\d+)\.", version)
        if not match:
            raise ValueError("Unable to verify the PostgreSQL major version")
        versions.append(match[1])
    if versions[0] != versions[1]:
        raise ValueError(
            "A PostgreSQL major upgrade needs a separate cluster migration"
        )
    helper = copy.deepcopy(old)
    operator = configuration(ROOT)
    helper_image = recovery.installed_images(operator)["migrate"]
    helper["services"]["migrate"]["image"] = helper_image
    helper_files = []
    for path in program_files(ROOT):
        relative = path.relative_to(ROOT)
        if relative.parts[0] != "backend":
            continue
        target = stage / "helper" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        helper_files.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": file_digest(path),
            }
        )
    images_for_helper = dict(manifest["images"])
    for name in ("backend", "worker", "sip_worker", "migrate", "checks"):
        images_for_helper[name] = helper_image
    # Check the helper's own lockfile independently from the incoming release.
    helper_probe = stage / "helper"
    (helper_probe / "frontend").mkdir()
    shutil.copyfile(
        stage / "program/frontend/package-lock.json",
        helper_probe / "frontend/package-lock.json",
    )
    recovery.check_dependencies(images_for_helper, helper_probe)
    env = old["services"]["postgres"]["environment"]
    if any(not IDENTIFIER.fullmatch(env[k]) for k in ("POSTGRES_USER", "POSTGRES_DB")):
        raise ValueError("Unsupported database identifier")
    if env["POSTGRES_DB"] in {"postgres", "template0", "template1"}:
        raise ValueError("The system databases cannot be updated")
    state = {
        "id": str(identity),
        "actor": str(actor),
        "root": str(root),
        "project": old["name"],
        "phase": "PREPARED",
        "reason": reason.strip(),
        "version": manifest["version"],
        "package_sha256": file_digest(stage / "release.zip"),
        "helper_files": helper_files,
        "database": env["POSTGRES_DB"],
        "owner": env["POSTGRES_USER"],
        "failed_database": "dispatcher_failed_" + identity.hex[:12],
        "compose_hashes": {},
    }
    if deployment:
        state.update(
            deployment=str(deployment),
            deployment_original_sha256=file_digest(deployment),
        )
    state["retained_images"] = retain_images(
        identity, {"old": old, "new": candidate, "helper": helper}
    )
    for name, value in (("old", old), ("new", candidate), ("helper", helper)):
        path = stage / f"{name}-compose.json"
        recovery.save(path, value)
        state["compose_hashes"][name] = file_digest(path)
    database_bytes = int(
        sql(stage, state, "SELECT pg_database_size(current_database())")
    )
    source_bytes = int(
        composition(
            stage,
            state,
            "old",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python3",
            "backup",
            "-c",
            "from pathlib import Path; print(sum(p.stat().st_size for p in Path('/source').rglob('*') if p.is_file()))",
        )
    )
    program_bytes = sum(item["size"] for item in manifest["files"])
    recovery.require_disk_space(
        source_bytes + database_bytes + program_bytes,
        2,
        host_bytes=3 * program_bytes + database_bytes,
        workspace=root,
    )
    files.prepare_files(root, stage, public_key, key)
    save(stage, state, key)
    return stage, state, key


def backup(stage, state, key):
    if "backup" not in state:
        result = composition(
            stage,
            state,
            "old",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python3",
            "backup",
            "/scripts/full_backup.py",
        )
        state["backup"] = json.loads(result.splitlines()[-1])["name"]
        if not SNAPSHOT_NAME.fullmatch(state["backup"]):
            raise ValueError("Invalid backup result")
        save(stage, state, key)
    composition(
        stage,
        state,
        "old",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python3",
        "backup",
        "/scripts/full_backup.py",
        "--verify",
        state["backup"],
    )
    inspect = (
        "import json,sys; from pathlib import Path; from app.operations.snapshot import read_manifest; "
        "m=read_manifest(Path('/backups'),sys.argv[1],Path('/config/.secrets/backup-signing.key').read_bytes().strip()); "
        "print(json.dumps(next(x for x in m['files'] if x['scope']=='database')))"
    )
    item = json.loads(
        composition(
            stage,
            state,
            "old",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python3",
            "backup",
            "-c",
            inspect,
            state["backup"],
        )
    )
    dump = stage / "before.dump"
    if not dump.exists():
        export = "import shutil,sys; from pathlib import Path; shutil.copyfileobj((Path('/backups/objects')/sys.argv[1]).open('rb'),sys.stdout.buffer)"
        temporary = dump.with_suffix(".partial")
        with temporary.open("wb") as output:
            composition(
                stage,
                state,
                "old",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python3",
                "backup",
                "-c",
                export,
                item["sha256"],
                output=output,
            )
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        if file_digest(temporary) != item["sha256"]:
            raise ValueError("Backup database export failed its digest check")
        temporary.replace(dump)
    if file_digest(dump) != item["sha256"]:
        raise ValueError("The saved database backup is damaged")
    state["dump_sha256"] = item["sha256"]
    save(stage, state, key)


def apply(stage, state, key):
    if state["phase"] not in {
        "PREPARED",
        "QUIESCED",
        "BACKED_UP",
        "FILES_APPLIED",
        "MIGRATED",
        "READY",
    }:
        raise ValueError("This update cannot be applied or resumed")
    root = Path(state["root"])
    no_oneoffs(state["project"])
    state["begin_intent"] = True
    save(stage, state, key)
    begun = database(
        stage,
        state,
        "begin",
        "--version",
        state["version"],
        "--digest",
        state["package_sha256"],
        "--reason",
        state["reason"],
    )
    if begun["phase"] == "ACTIVE":
        publish_deployment(stage, state, key, "new")
        state["phase"] = "ACTIVE"
        save(stage, state, key)
        return state
    state["begun"] = True
    save(stage, state, key)
    if state["phase"] == "PREPARED":
        publish_deployment(stage, state, key, "old")
    composition(stage, state, "old", "stop", *WRITERS)
    if state["phase"] == "PREPARED":
        state["phase"] = "QUIESCED"
        save(stage, state, key)
    if state["phase"] == "QUIESCED":
        backup(stage, state, key)
        advance(stage, state, "BACKED_UP", {"snapshot": state["backup"]})
        state["phase"] = "BACKED_UP"
        save(stage, state, key)
    if state["phase"] == "BACKED_UP":
        files.apply_files(root, stage, key)
        state["phase"] = "FILES_APPLIED"
        save(stage, state, key)
    if state["phase"] == "FILES_APPLIED":
        composition(stage, state, "new", "run", "--rm", "--no-deps", "migrate")
        revision = sql(stage, state, "SELECT version_num FROM alembic_version")
        advance(stage, state, "MIGRATED", {"revision": revision})
        state["phase"] = "MIGRATED"
        save(stage, state, key)
    if state["phase"] == "MIGRATED":
        start(stage, state, "new", force=True)
        advance(stage, state, "READY", {"health_checked": True})
        state["phase"] = "READY"
        save(stage, state, key)
    elif state["phase"] == "READY":
        start(stage, state, "new")
    return state


def restore_database(stage, state, key):
    original, failed = state["database"], state["failed_database"]
    if not IDENTIFIER.fullmatch(original) or not re.fullmatch(
        r"dispatcher_failed_[a-f0-9]{12}", failed
    ):
        raise ValueError("Invalid rollback database identifiers")
    if file_digest(stage / "before.dump") != state["dump_sha256"]:
        raise ValueError("The rollback dump is damaged")
    exists = sql(
        stage,
        state,
        f"SELECT datname FROM pg_database WHERE datname IN ('{original}','{failed}')",
        database_name="postgres",
    ).splitlines()
    marker = "dispatcher-update-rollback:" + state["id"]
    if failed not in exists:
        sql(
            stage,
            state,
            f'ALTER DATABASE "{original}" ALLOW_CONNECTIONS false',
            database_name="postgres",
        )
        sql(
            stage,
            state,
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{original}'",
            database_name="postgres",
        )
        sql(
            stage,
            state,
            f'ALTER DATABASE "{original}" RENAME TO "{failed}"',
            database_name="postgres",
        )
    elif original in exists and not state.get("database_restored"):
        actual = sql(
            stage,
            state,
            f"SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname='{original}'",
            database_name="postgres",
        )
        if actual != marker:
            raise ValueError("The rollback target is not a verified partial restore")
        composition(
            stage,
            state,
            "old",
            "exec",
            "-T",
            "postgres",
            "dropdb",
            "--username",
            state["owner"],
            "--force",
            original,
        )
    if not state.get("database_restored"):
        composition(
            stage,
            state,
            "old",
            "exec",
            "-T",
            "postgres",
            "createdb",
            "--username",
            state["owner"],
            "--template",
            "template0",
            original,
        )
        sql(
            stage,
            state,
            f"COMMENT ON DATABASE \"{original}\" IS '{marker}'",
            database_name="postgres",
        )
        composition(
            stage,
            state,
            "old",
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "--username",
            state["owner"],
            "--dbname",
            original,
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            incoming=stage / "before.dump",
        )
        state["database_restored"] = True
        save(stage, state, key)


def rollback(stage, state, key):
    if state["phase"] in {"ACTIVE", "ACTIVATING"}:
        raise ValueError(
            "An activated release needs a new compatible update; later training data must be preserved"
        )
    if state["phase"] == "ROLLED_BACK":
        return state
    no_oneoffs(state["project"])
    exported = stage / "audit.json"
    tail_saved = state["phase"] == "ROLLING_BACK" and exported.exists()
    if tail_saved:
        tail = files.read_record(
            exported, key, "software-update-audit-1", limit=66 * 1024 * 1024
        )
        if (
            tail["update"]["id"] != state["id"]
            or tail["update"]["package_sha256"] != state["package_sha256"]
        ):
            raise ValueError("The saved audit belongs to another update")
    else:
        try:
            phase = (
                database(stage, state, "status")["phase"]
                if state.get("begin_intent") or state.get("begun")
                else "ABSENT"
            )
        except RuntimeError:
            if not state.get("begun"):
                raise
            phase = None  # The old PostgreSQL configuration must be restored before reading it.
        if phase == "ACTIVE":
            raise ValueError(
                "The database already activated this release; rollback was refused"
            )
        if phase in {"ABSENT", "ROLLED_BACK"}:
            state["phase"] = "ROLLED_BACK"
            save(stage, state, key)
            return state
        state["begun"] = True
    composition(stage, state, "new", "stop", *WRITERS)
    files.rollback_files(Path(state["root"]), stage, key)
    composition(
        stage,
        state,
        "old",
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "postgres",
        "redis",
    )
    if state.get("begun"):
        if not tail_saved:
            phase = database(stage, state, "status")["phase"]
            if phase != "ROLLING_BACK":
                advance(stage, state, "ROLLING_BACK")
        state["phase"] = "ROLLING_BACK"
        save(stage, state, key)
        if state.get("dump_sha256"):
            if not (stage / "audit.json").exists():
                database(
                    stage,
                    state,
                    "export-audit",
                    "--file",
                    "/update/audit.json",
                    "--signing-key",
                    "/signing-key",
                )
            restore_database(stage, state, key)
            database(
                stage,
                state,
                "merge-audit",
                "--file",
                "/update/audit.json",
                "--signing-key",
                "/signing-key",
            )
            composition(stage, state, "old", "run", "--rm", "--no-deps", "migrate")
        start(stage, state, "old", force=True)
        publish_deployment(stage, state, key, "old")
        advance(stage, state, "ROLLED_BACK")
    else:
        start(stage, state, "old")
    state["phase"] = "ROLLED_BACK"
    save(stage, state, key)
    return state


def activate(stage, state, key):
    if state["phase"] == "ACTIVE":
        return state
    if state["phase"] not in {"READY", "ACTIVATING"}:
        raise ValueError("Only a checked release can be activated")
    files.verify_tree(
        Path(state["root"]), files.transaction(Path(state["root"]), stage, key)["after"]
    )
    start(stage, state, "new")
    state["phase"] = "ACTIVATING"
    save(stage, state, key)
    publish_deployment(stage, state, key, "new")
    advance(stage, state, "ACTIVE")
    state["phase"] = "ACTIVE"
    save(stage, state, key)
    return state


def perform(stage, state, key, action):
    try:
        if action in ("apply", "resume"):
            return apply(stage, state, key)
        if action == "rollback":
            return rollback(stage, state, key)
        if action == "activate":
            return activate(stage, state, key)
        return state
    except Exception:
        if action in ("apply", "resume") and state.get("begun"):
            rollback(stage, state, key)
        raise


def operator_request(root, request_file, archive):
    deployment = configuration(root)
    session_secret = deployment["services"]["backend"]["environment"].get("JWT_SECRET")
    key = catalog.request_key(
        (root / ".secrets/backup-control/token").read_bytes().strip(), session_secret
    )
    value = catalog.read_request(request_file, key)
    public_key = catalog.publisher(root / ".secrets/software-publisher.pub")
    if (
        public_key is None
        or hashlib.sha256(public_key).hexdigest() != value["publisher_sha256"]
    ):
        raise ValueError("The request belongs to a different publisher key")
    if value["action"] == "apply":
        if archive is None or file_digest(archive) != value["package_sha256"]:
            raise ValueError("Select the exact package verified by the administrator")
        if unpack_package(archive, public_key)["version"] != value["version"]:
            raise ValueError("The software version differs from the signed request")
    elif archive is not None:
        raise ValueError(
            "Activation and rollback use the saved installation transaction"
        )
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "apply",
            "resume",
            "activate",
            "rollback",
            "status",
            "execute-request",
        ),
    )
    parser.add_argument("--installation", type=Path, default=ROOT)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--trust-key", type=Path)
    parser.add_argument("--actor", type=UUID)
    parser.add_argument("--reason")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    root = args.installation.resolve()
    with installation_lock(root):
        request = None
        if args.action == "execute-request":
            if args.request is None or any(
                (args.actor, args.reason, args.trust_key, args.target)
            ):
                parser.error(
                    "execute-request requires --request and uses its signed actor, reason and target"
                )
            request = operator_request(root, args.request, args.package)
            args.action = request["action"]
            args.actor = UUID(request["actor_id"])
            args.reason = request["reason"]
            args.trust_key = root / ".secrets/software-publisher.pub"
            args.target = (
                root / ".updates" / ("install-" + UUID(request["update_id"]).hex)
            )
        if args.action == "apply":
            if not all((args.package, args.trust_key, args.actor, args.reason)):
                parser.error(
                    "apply requires --package, --trust-key, --actor and --reason"
                )
            stage, state, key = initial_state(
                root,
                args.package,
                args.trust_key.read_bytes(),
                args.actor,
                args.reason,
                args.deployment.resolve() if args.deployment else None,
                identity=UUID(request["update_id"]) if request else None,
            )
        else:
            if args.target is None:
                parser.error("this action requires --target")
            stage = args.target.resolve()
            files.checked_directories(root, stage)
            key = (root / ".secrets/backup-signing.key").read_bytes().strip()
            state = files.read_record(stage / STATE, key, PURPOSE)
            if state["root"] != str(root) or not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,62}", state["project"]
            ):
                raise ValueError("The update belongs to another installation")
            if request:
                if (
                    state["id"] != request["update_id"]
                    or state["version"] != request["version"]
                    or state["package_sha256"] != request["package_sha256"]
                ):
                    raise ValueError(
                        "The request belongs to a different installed transaction"
                    )
                state["actor"] = request["actor_id"]
                database(stage, state, "status")
        print(json.dumps({"target": str(stage), "phase": state["phase"]}), flush=True)
        perform(stage, state, key, args.action)
        print(
            json.dumps(
                {
                    "id": state["id"],
                    "phase": state["phase"],
                    "target": str(stage),
                    "project": state["project"],
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(f"Software installation failed: {error}") from None
