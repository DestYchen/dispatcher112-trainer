"""Recover a complete, separate local installation using bounded one-shot Docker CLI calls."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_DISK_RESERVE = 4 * 1024 * 1024 * 1024
sys.path.insert(0, str(ROOT / "backend"))

# The installer uses this workspace's modules without installing the application on the host.
from app.operations.recovery_compose import PROJECT, recovery_compose  # noqa: E402
from app.operations.snapshot import checked_relative, read_manifest  # noqa: E402


def run(*arguments, input_text=None, cwd=ROOT):
    completed = subprocess.run(
        arguments,
        input=input_text,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise RuntimeError(
            f"{arguments[0]} failed with exit code {completed.returncode}; recovery data preserved"
        )
    return completed.stdout.strip()


def save(path, value):
    temporary = path.with_suffix(".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def docker_storage_path():
    context = os.environ.get("DOCKER_CONTEXT")
    endpoint = os.environ.get("DOCKER_HOST") if not context else None
    if not endpoint:
        endpoint = json.loads(
            run(
                "docker",
                "context",
                "inspect",
                context or run("docker", "context", "show"),
                "--format",
                "{{json .Endpoints.docker.Host}}",
            )
        )
    local_endpoint = "npipe:////./pipe/" if sys.platform == "win32" else "unix://"
    if not endpoint.startswith(local_endpoint):
        raise ValueError(
            "Recovery requires a local Docker engine; remote storage cannot be checked"
        )
    kind, operating_system, directory = run(
        "docker",
        "info",
        "--format",
        "{{.OSType}}|{{.OperatingSystem}}|{{.DockerRootDir}}",
    ).split("|", 2)
    if kind != "linux":
        raise ValueError("Recovery requires Linux containers")
    if sys.platform == "win32" and operating_system == "Docker Desktop":
        settings_directory = Path(os.environ["APPDATA"]) / "Docker"
        settings_path = settings_directory / "settings-store.json"
        if not settings_path.exists():
            settings_path = settings_directory / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        if not settings.get("WslEngineEnabled", settings.get("wslEngineEnabled", True)):
            raise ValueError(
                "Recovery disk checks on Windows require the Docker WSL2 backend"
            )
        directory = settings.get("CustomWslDistroDir") or settings.get(
            "customWslDistroDir"
        )
        storage = (
            Path(directory)
            if directory
            else Path(os.environ["LOCALAPPDATA"]) / "Docker/wsl"
        )
    elif sys.platform == "linux" and operating_system != "Docker Desktop":
        storage = Path(directory)
    else:
        raise ValueError(
            "Recovery supports local Docker WSL2 on Windows or native Docker on Linux"
        )
    if not storage.is_absolute() or not storage.is_dir():
        raise ValueError(
            "Docker storage directory is unavailable; no recovery data was written"
        )
    return storage.resolve()


def require_disk_space(snapshot_bytes, copies, *, host_bytes=0, workspace=None):
    requirements = {}
    for path, size in (
        (workspace or ROOT, host_bytes),
        (docker_storage_path(), copies * snapshot_bytes),
    ):
        filesystem = path.stat().st_dev
        if filesystem in requirements:
            requirements[filesystem][1] += size
        else:
            requirements[filesystem] = [path, size + RECOVERY_DISK_RESERVE]
    for path, required in requirements.values():
        available = shutil.disk_usage(path).free
        if available < required:
            raise ValueError(
                f"Recovery requires {required} free host bytes on {path} including reserve; "
                f"only {available} are available. Existing data and services are preserved."
            )


def checked_target(value):
    target = Path(value).resolve()
    directory = ROOT / ".recovery"
    if not target.is_relative_to(directory) or target == directory:
        raise ValueError(
            "Recovery targets must be new directories below this workspace's .recovery"
        )
    if "," in str(target) or "," in str(ROOT):
        raise ValueError("Recovery mount paths must not contain commas")
    for path in (target, *target.parents):
        if path.is_symlink():
            raise ValueError("Recovery paths must not contain symbolic links")
        if path == ROOT:
            break
    return target


def helper(image, project, *, archive=False, snapshot=None):
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--workdir",
        "/installer",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--env",
        "PYTHONPATH=/installer",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--mount",
        f"type=bind,source={ROOT / 'backend'},target=/installer,readonly",
        "--mount",
        f"type=volume,source={project}_files,target=/recovery"
        + (",readonly" if archive else ""),
    ]
    if not archive:
        command += [
            "--mount",
            f"type=bind,source={ROOT / '.backups'},target=/backups,readonly",
            "--mount",
            f"type=bind,source={ROOT / '.secrets/backup-signing.key'},target=/signing-key,readonly",
        ]
    command += [image, "python", "-m", "app.operations.recovery_files"]
    command += ["--archive"] if archive else ["--snapshot", snapshot]
    return command


def extract_host(command, workspace):
    workspace.mkdir(mode=0o700)
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                relative = checked_relative(member.name)
                destination = workspace / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("Missing recovery archive member")
                    with source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    destination.chmod(
                        0o600
                        if ".secrets" in relative.parts or relative.name == ".env"
                        else 0o644
                    )
                else:
                    raise ValueError(
                        "Recovery archives may contain only regular files and directories"
                    )
        if process.wait() != 0:
            raise RuntimeError("Could not export the recovered workspace")
    finally:
        if process.stdout:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
            process.wait()


def compose(target, *arguments, input_text=None):
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    if not PROJECT.fullmatch(state["project"]):
        raise ValueError("Invalid recovery state")
    import compose as installed_compose

    workspace = target / "workspace"
    if installed_compose.deployment(workspace):
        return run(
            *installed_compose.command(workspace, list(arguments)),
            input_text=input_text,
        )
    return run(
        "docker",
        "compose",
        "-p",
        state["project"],
        "-f",
        str(target / "compose.json"),
        *arguments,
        input_text=input_text,
    )


def database(target, action, *extra):
    return json.loads(
        compose(
            target,
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            f"{ROOT / 'backend/app/operations/recovery_database.py'}:/installer/recovery_database.py:ro",
            "migrate",
            "python",
            "/installer/recovery_database.py",
            action,
            *extra,
        ).splitlines()[-1]
    )


def installed_images(source):
    images = {}
    for name, service in source["services"].items():
        if name == "control":
            continue
        tag = service.get("image") or f"{source['name']}-{name}"
        images[name] = run("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    return images


def check_dependencies(images, workspace):
    for name in ("backend", "worker", "sip_worker", "migrate", "checks"):
        expected = hashlib.sha256(
            (workspace / "backend/requirements.lock").read_bytes()
        ).hexdigest()
        actual = run(
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
            "python",
            images[name],
            "-c",
            "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/app/requirements.lock').read_bytes()).hexdigest())",
        )
        if actual != expected:
            raise ValueError(
                f"The installed {name} image does not match the archived Python lock file"
            )
    for name in ("frontend", "frontend_checks"):
        expected = hashlib.sha256(
            (workspace / "frontend/package-lock.json").read_bytes()
        ).hexdigest()
        actual = run(
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
            "node",
            images[name],
            "-e",
            "console.log(require('crypto').createHash('sha256').update(require('fs').readFileSync('/app/package-lock.json')).digest('hex'))",
        )
        if actual != expected:
            raise ValueError(
                f"The installed {name} image does not match the archived npm lock file"
            )


def prepare(arguments, target):
    if target.exists():
        raise ValueError(
            "The recovery target already exists; its data will not be overwritten"
        )
    key = (ROOT / ".secrets/backup-signing.key").read_bytes().strip()
    manifest = read_manifest(ROOT / ".backups", arguments.snapshot, key)
    if manifest["schema"] != "dispatcher-backup-2":
        raise ValueError("This snapshot has no application source")
    project = "dispatcher_recovery_" + uuid4().hex[:12]
    image = run(
        "docker", "image", "inspect", "--format", "{{.Id}}", "dispatcher112-backend"
    )
    host_bytes = sum(
        item["size"]
        for item in manifest["files"]
        if item["scope"] in {"program", "config"}
        or item["scope"] == "data"
        and item["path"].split("/")[0] != "llm"
    )
    require_disk_space(
        sum(item["size"] for item in manifest["files"]), 2, host_bytes=host_bytes
    )
    target.mkdir(parents=True, mode=0o700)
    state = {
        "project": project,
        "snapshot": arguments.snapshot,
        "status": "EXTRACTING",
        "ui_port": arguments.ui_port,
        "api_port": arguments.api_port,
        "created_at": datetime.now(UTC).isoformat(),
    }
    save(target / "state.json", state)
    try:
        run(
            "docker",
            "volume",
            "create",
            "--label",
            f"dispatcher.recovery={project}",
            f"{project}_files",
        )
        print("Extracting the signed snapshot into a separate volume", flush=True)
        extraction = json.loads(
            run(*helper(image, project, snapshot=arguments.snapshot))
        )
        workspace = target / "workspace"
        extract_host(helper(image, project, archive=True), workspace)
        (workspace / "data/llm/models").mkdir(parents=True, exist_ok=True)
        for path in (
            workspace / "backend/app/operations/recovery_database.py",
            workspace / "docker-compose.tls.yml",
        ):
            if not path.is_file():
                raise ValueError(
                    "Snapshot predates the recovery protocol or TLS deployment; create a current snapshot"
                )
        source = json.loads(
            run(
                "docker",
                "compose",
                "--project-directory",
                str(workspace),
                "--env-file",
                str(workspace / ".env"),
                "--profile",
                "*",
                "-f",
                str(workspace / "docker-compose.yml"),
                "-f",
                str(workspace / "docker-compose.tls.yml"),
                "config",
                "--format",
                "json",
            )
        )
        images = installed_images(source)
        check_dependencies(images, workspace)
        deployment = recovery_compose(
            source,
            workspace,
            project,
            images,
            arguments.ui_port,
            arguments.api_port,
            arguments.media_address,
        )
        save(target / "compose.json", deployment)
        compose(target, "config", "--quiet")
        save(target / "extraction.json", extraction)
        print(
            "Preparing separate PostgreSQL, Redis and runtime credentials", flush=True
        )
        compose(
            target,
            "up",
            "--detach",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "120",
            "postgres",
            "redis",
        )
        compose(target, "run", "--rm", "--no-deps", "prepare_runtime")
        environment = deployment["services"]["postgres"]["environment"]
        compose(
            target,
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--username",
            environment["POSTGRES_USER"],
            "--dbname",
            environment["POSTGRES_DB"],
            "/recovery-database/database.dump",
        )
        compose(target, "run", "--rm", "--no-deps", "migrate")
        state["database"] = database(
            target, "prepare", "--snapshot", arguments.snapshot, "--instance", project
        )
        state["status"] = "STARTING"
        save(target / "state.json", state)
        print("Starting all recovered services with maintenance enabled", flush=True)
        compose(
            target, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "240"
        )
        state["status"] = "PREPARED"
        save(target / "state.json", state)
        print(
            json.dumps(
                {
                    "project": project,
                    "status": state["status"],
                    "url": f"https://localhost:{arguments.ui_port}",
                }
            )
        )
    except BaseException:
        state["status"] = "FAILED"
        save(target / "state.json", state)
        raise


def remove(target, state):
    project = state["project"]
    if not PROJECT.fullmatch(project):
        raise ValueError("Invalid recovery project")
    volume = project + "_files"
    metadata = json.loads(run("docker", "volume", "inspect", volume))[0]
    if metadata.get("Labels", {}).get("dispatcher.recovery") != project:
        raise ValueError("This volume does not belong to the selected recovery")
    if (target / "compose.json").is_file():
        compose(target, "down", "--volumes", "--remove-orphans")
    run("docker", "volume", "rm", volume)
    state["status"] = "REMOVED"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("prepare", "activate", "stop", "start", "remove")
    )
    parser.add_argument("--target", required=True)
    parser.add_argument("--snapshot")
    parser.add_argument("--ui-port", type=int, default=15173)
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--media-address", default="127.0.0.2")
    parser.add_argument("--delete-data", action="store_true")
    arguments = parser.parse_args()
    target = checked_target(arguments.target)
    if arguments.action == "prepare":
        if not arguments.snapshot:
            parser.error("prepare requires --snapshot")
        prepare(arguments, target)
        return
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    if arguments.action == "remove":
        if not arguments.delete_data:
            parser.error(
                "remove deletes only this recovered instance; supply --delete-data"
            )
        remove(target, state)
    elif arguments.action == "stop":
        compose(target, "stop")
        state["stopped"] = True
    elif arguments.action == "start":
        if state["status"] not in {"PREPARED", "ACTIVE"}:
            raise ValueError("Only a completed recovery can be started")
        compose(
            target, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "240"
        )
        state["stopped"] = False
    else:
        if state["status"] not in {"PREPARED", "ACTIVE"} or state.get("stopped"):
            raise ValueError("Activate only a running, fully prepared recovery")
        if not state.get("backup_schedule_restored"):
            key = (ROOT / ".secrets/backup-signing.key").read_bytes().strip()
            manifest = read_manifest(ROOT / ".backups", state["snapshot"], key)
            require_disk_space(sum(item["size"] for item in manifest["files"]), 1)
        compose(
            target, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "240"
        )
        state["database"] = database(target, "activate")
        state["status"] = "ACTIVE"
        save(target / "state.json", state)
        if not state.get("backup_schedule_restored"):
            extraction = json.loads(
                (target / "extraction.json").read_text(encoding="utf-8")
            )
            compose(
                target,
                "exec",
                "-T",
                "backup",
                "python3",
                "-c",
                "import json,sys; from pathlib import Path; "
                "from app.operations.backup_protocol import validate_schedule,write_signed; "
                "write_signed(Path('/queue/schedule.json'),validate_schedule(json.load(sys.stdin)),"
                "Path('/config/.secrets/backup-control/token').read_bytes().strip(),'schedule')",
                input_text=json.dumps(extraction["original_schedule"]),
            )
            state["backup_schedule_restored"] = True
    save(target / "state.json", state)
    print(
        json.dumps(
            {
                "project": state["project"],
                "status": state["status"],
                "stopped": state.get("stopped", False),
            }
        )
    )


if __name__ == "__main__":
    main()
