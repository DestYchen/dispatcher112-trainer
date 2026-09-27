"""Collect service status or execute one signed UI request, using local Docker CLI."""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import compose

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.operations.service_policy import MIN_MEMORY, RESTART_ONLY, SERVICES  # noqa: E402


def run(arguments, *, incoming=None, timeout=180):
    completed = subprocess.run(
        arguments,
        input=incoming,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Local command failed: {arguments[0]} (exit {completed.returncode})"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def project_name(root):
    selected = compose.deployment(root)
    if selected:
        return selected[1]
    value = json.loads(run(compose.command(root, ["config", "--format", "json"])))
    project = value["name"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("Invalid Compose project")
    return project


def containers(project):
    ids = run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.oneoff=False",
        ]
    ).split()
    if not ids:
        raise ValueError("Installation containers are missing")
    result = json.loads(run(["docker", "inspect", *ids]))
    return [
        item
        for item in result
        if item["Config"]["Labels"].get("com.docker.compose.service") in SERVICES
        and item["Config"]["Labels"].get("com.docker.compose.project") == project
        and item["Config"]["Labels"].get("com.docker.compose.oneoff") == "False"
    ]


def bridge(project, action, payload):
    backend = next(
        item
        for item in containers(project)
        if item["Config"]["Labels"]["com.docker.compose.service"] == "backend"
        and item["State"]["Running"]
    )
    return json.loads(
        run(
            [
                "docker",
                "exec",
                "-i",
                backend["Id"],
                "python",
                "-m",
                "app.operations.host_exchange",
                action,
            ],
            incoming=json.dumps(payload).encode("utf-8"),
        )
    )


def memory_bytes(value):
    match = re.fullmatch(r"([0-9.]+)\s*(B|KiB|MiB|GiB|TiB|kB|MB|GB)", value.strip())
    if not match:
        return None
    units = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
    }
    return int(float(match[1]) * units[match[2]])


def collect(project, root=ROOT, *, interval=None):
    rows = containers(project)
    selected = compose.deployment(root)
    pinned = (
        json.loads(selected[0].read_text(encoding="utf-8"))["services"]
        if selected
        else {}
    )
    expected = {
        name: definition.get("image")
        for name, definition in pinned.items()
        if re.fullmatch(r"sha256:[a-f0-9]{64}", definition.get("image", ""))
    }
    if not expected:
        expected = compose.offline_images(root)
    running = [item["Id"] for item in rows if item["State"]["Running"]]
    samples = {}
    if running:
        raw = run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *running]
        )
        samples = {sample["ID"]: sample for sample in map(json.loads, raw.splitlines())}
    secret_values = set()
    for item in rows:
        for variable in item["Config"].get("Env", []):
            name, _, value = variable.partition("=")
            if value and any(
                part in name.upper()
                for part in ("PASSWORD", "SECRET", "TOKEN", "DATABASE_URL")
            ):
                secret_values.add(value)
    services, logs = [], {}
    for item in rows:
        identity = item["Id"]
        service = item["Config"]["Labels"]["com.docker.compose.service"]
        sample = samples.get(identity[:12], samples.get(identity, {}))
        services.append(
            {
                "id": identity[:12],
                "service": service,
                "state": item["State"]["Status"],
                "health": item["State"].get("Health", {}).get("Status"),
                "image_id": item["Image"],
                "image_matches": item["Image"] == expected[service]
                if service in expected
                else None,
                "readonly_root": item["HostConfig"].get("ReadonlyRootfs", False),
                "cpu_percent": float(sample["CPUPerc"].rstrip("%")) if sample else None,
                "memory_bytes": memory_bytes(sample["MemUsage"].split("/")[0])
                if sample
                else None,
                "memory_limit_bytes": item["HostConfig"]["Memory"],
                "cpu_limit": item["HostConfig"].get("NanoCpus", 0) / 1_000_000_000,
                "minimum_memory_mb": MIN_MEMORY.get(service, 128),
                "actions": ["restart"]
                if service in RESTART_ONLY
                else ["start", "stop", "restart"],
            }
        )
        completed = subprocess.run(
            ["docker", "logs", "--tail", "100", "--timestamps", identity],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        content = (completed.stdout + completed.stderr).decode(
            "utf-8", errors="replace"
        )
        for secret in sorted(secret_values, key=len, reverse=True):
            content = content.replace(secret, "[скрыто]")
        logs.setdefault(service, []).append(
            {"instance": identity[:12], "text": content[-16384:]}
        )
    info = json.loads(run(["docker", "info", "--format", "{{json .}}"]))
    return bridge(
        project,
        "publish",
        {
            "services": {
                "items": services,
                "refresh_interval_sec": interval,
                "host": {
                    "cpus": info["NCPU"],
                    "memory_bytes": info["MemTotal"],
                    "engine_version": info["ServerVersion"],
                },
            },
            "logs": logs,
        },
    )


def watch(root, interval, *, token=None):
    """Read Docker state repeatedly; never execute requests or change containers."""
    while True:
        if token:
            import service_monitor

            if not service_monitor.active(root, token):
                return
        started = time.monotonic()
        try:
            result = collect(project_name(root), root, interval=interval)
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            StopIteration,
            subprocess.TimeoutExpired,
        ) as error:
            # Never re-date an old report as fresh when Docker/backend is unavailable.
            result = {"status": "unavailable", "error": type(error).__name__}
        if token:
            service_monitor.heartbeat(root, token, result)
        else:
            print(json.dumps(result), flush=True)
        time.sleep(max(1, interval - (time.monotonic() - started)))


def wait_service(identity, stopped):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = json.loads(run(["docker", "inspect", identity]))[0]["State"]
        if (
            not state["Running"]
            if stopped
            else state["Running"]
            and state.get("Health", {}).get("Status", "healthy") == "healthy"
        ):
            return
        time.sleep(1)
    raise RuntimeError("Service did not reach the expected state within three minutes")


def execute(root, project, path):
    if path.stat().st_size > 8192 or path.is_symlink():
        raise ValueError("Invalid local request file")
    job = bridge(project, "begin", json.loads(path.read_text(encoding="utf-8")))
    if job["status"] in {"SUCCEEDED", "FAILED"}:
        return {"id": job["id"], "status": job["status"], "repeated": True}
    request = job["request"]
    try:
        rows = [
            row
            for row in containers(project)
            if row["Config"]["Labels"]["com.docker.compose.service"]
            == request["service"]
        ]
        if not rows:
            raise ValueError("Service container is missing")
        if request["kind"] == "service":
            for item in rows:
                run(["docker", request["action"], item["Id"]])
                wait_service(item["Id"], request["action"] == "stop")
        else:
            # Persist first: retries and subsequent Compose recreation use the same limits.
            compose.save_resource_limits(
                root, request["service"], request["cpus"], request["memory_mb"]
            )
            for item in rows:
                run(
                    [
                        "docker",
                        "update",
                        "--cpus",
                        str(request["cpus"]),
                        "--memory",
                        f"{request['memory_mb']}m",
                        "--memory-swap",
                        f"{request['memory_mb']}m",
                        item["Id"],
                    ]
                )
        result = bridge(
            project,
            "finish",
            {
                "id": job["id"],
                "status": "SUCCEEDED",
                "result": {"service": request["service"], "instances": len(rows)},
            },
        )
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError):
        try:
            bridge(project, "finish", {"id": job["id"], "status": "FAILED"})
        except (
            RuntimeError,
            ValueError,
            subprocess.TimeoutExpired,
            OSError,
            StopIteration,
        ):
            print(
                f"Result pending: restore backend and use resolve-failed --job {job['id']}",
                file=sys.stderr,
            )
        raise
    collect(project, root)
    return {"id": result["id"], "status": result["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["collect", "watch", "execute-request", "resolve-failed"]
    )
    parser.add_argument("--installation", type=Path, default=ROOT)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--job")
    parser.add_argument(
        "--interval", type=int, choices=range(5, 61), default=10, metavar="5..60"
    )
    args = parser.parse_args()
    root = args.installation.resolve()
    if args.action == "watch":
        watch(root, args.interval)
        return
    project = project_name(root)
    if args.action == "collect":
        result = collect(project, root)
    elif args.action == "execute-request":
        if args.request is None:
            parser.error("--request is required")
        result = execute(root, project, args.request)
    else:
        if not args.job:
            parser.error("--job is required after inspecting an interrupted operation")
        result = bridge(project, "finish", {"id": args.job, "status": "FAILED"})
        result = {"id": result["id"], "status": result["status"]}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Monitoring stopped; project services remain running.")
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        StopIteration,
        subprocess.TimeoutExpired,
    ) as error:
        raise SystemExit(
            f"Technical operation stopped ({type(error).__name__}); no secrets printed"
        ) from None
