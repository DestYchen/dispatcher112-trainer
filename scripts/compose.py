"""Run Compose with the installed release's signed, pinned deployment when present."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.operations.backup_protocol import read_signed  # noqa: E402
from app.operations.service_policy import valid_limits  # noqa: E402

PURPOSE = "software-active-deployment-1"


def resource_limits(root):
    path = root / "data/materials/.technical-host/resource-limits.json"
    if not path.exists():
        return {}
    if path.resolve() != path or path.stat().st_size > 65536:
        raise ValueError("Invalid resource configuration path")
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or any(
        not isinstance(item, dict)
        or set(item) != {"cpus", "memory_mb"}
        or not valid_limits(name, item["cpus"], item["memory_mb"])
        for name, item in values.items()
    ):
        raise ValueError("Invalid persistent resource limits")
    return values


def save_resource_limits(root, service, cpus, memory_mb):
    if not valid_limits(service, cpus, memory_mb):
        raise ValueError("Resource limits are outside the permitted range")
    values = resource_limits(root)
    values[service] = {"cpus": cpus, "memory_mb": memory_mb}
    path = root / "data/materials/.technical-host/resource-limits.json"
    if path.resolve() != path or path.parent.resolve() != path.parent:
        raise ValueError("Resource configuration cannot use links")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    if temporary.is_symlink():
        raise ValueError("Resource configuration cannot use links")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(values, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def resource_overlay(root):
    values = resource_limits(root)
    route = gateway_route(root)
    replicas = backend_replicas(root)
    images = (
        offline_images(root) if not (root / ".updates/active.json").exists() else {}
    )
    if not values and route is None and replicas is None and not images:
        return None
    path = root / ".runtime-resources.compose.json"
    if path.resolve() != path:
        raise ValueError("Resource overlay cannot use links")
    document = {
        "services": {
            name: {
                "cpus": item["cpus"],
                "mem_limit": item["memory_mb"] * 1024 * 1024,
                "memswap_limit": item["memory_mb"] * 1024 * 1024,
                "deploy": {
                    "resources": {
                        "limits": {
                            "cpus": str(item["cpus"]),
                            "memory": str(item["memory_mb"] * 1024 * 1024),
                        }
                    }
                },
            }
            for name, item in values.items()
        }
    }
    for service, image in images.items():
        document["services"].setdefault(service, {}).update(
            image=image, pull_policy="never"
        )
    if replicas is not None:
        document["services"].setdefault("backend", {}).setdefault("deploy", {})[
            "replicas"
        ] = replicas
    if route is not None:
        document["services"].setdefault("gateway", {})["ports"] = route["ports"]
        for service in ("backend", "worker", "sip_worker", "checks"):
            document["services"].setdefault(service, {})["environment"] = {
                "CORS_ORIGINS": json.dumps(route["cors_origins"])
            }
        # Compose merges port lists. The YAML override tag removes old bindings.
        lines = ["services:"]
        for name, definition in document["services"].items():
            lines.append(f"  {name}:")
            for field, value in definition.items():
                tag = "!override " if name == "gateway" and field == "ports" else ""
                lines.append(f"    {field}: {tag}{json.dumps(value)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path.write_text(json.dumps(document), encoding="utf-8")
    return path


def backend_replicas(root):
    path = root / "data/materials/.technical-host/topology.json"
    if not path.exists():
        return None
    if path.resolve() != path or path.stat().st_size > 8192:
        raise ValueError("Invalid backend topology file")
    value = json.loads(path.read_text(encoding="utf-8"))
    replicas = (
        value.get("replicas")
        if value.get("enabled")
        else value.get("previous_replicas")
    )
    if replicas is None:
        return None
    if type(replicas) is not int or not 1 <= replicas <= 4:
        raise ValueError("Invalid backend replica count")
    return replicas


def offline_images(root):
    path = root / "data/materials/.technical-host/offline-images.json"
    if not path.exists():
        return {}
    if path.resolve() != path or path.stat().st_size > 16384:
        raise ValueError("Invalid offline image inventory")
    from app.operations.recovery_compose import SERVICES

    images = json.loads(path.read_text(encoding="utf-8"))
    if set(images) != SERVICES or any(
        not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value)
        for value in images.values()
    ):
        raise ValueError(
            "Offline image inventory must name all pinned application images"
        )
    return images


def gateway_route(root):
    path = root / "data/materials/.technical-host/gateway-route.json"
    if not path.exists():
        return None
    if path.resolve() != path or path.stat().st_size > 8192:
        raise ValueError("Invalid gateway route file")
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_gateway_route(value)
    return value


def validate_gateway_route(value):
    if set(value) != {"ports", "cors_origins"} or not isinstance(value["ports"], list):
        raise ValueError("Invalid gateway route")
    if len(value["ports"]) not in {0, 2}:
        raise ValueError("A gateway requires both UI and API ports")
    targets = set()
    for port in value["ports"]:
        if (
            set(port) != {"target", "published", "host_ip", "protocol"}
            or port["target"] not in {5173, 8000}
            or port["protocol"] != "tcp"
            or not str(port["published"]).isdigit()
            or not 1024 <= int(port["published"]) <= 65535
            or not ipaddress.ip_address(port["host_ip"]).is_loopback
        ):
            raise ValueError("Unsupported gateway binding")
        targets.add(port["target"])
    if value["ports"] and targets != {5173, 8000}:
        raise ValueError("Gateway targets must be distinct")
    if (
        not isinstance(value["cors_origins"], list)
        or not 1 <= len(value["cors_origins"]) <= 8
        or any(
            not isinstance(origin, str)
            or not re.fullmatch(
                r"https://(?:localhost|127\.0\.0\.1):[0-9]{4,5}", origin
            )
            for origin in value["cors_origins"]
        )
    ):
        raise ValueError("Only local HTTPS origins are supported for recovery")


def save_gateway_route(root, value):
    validate_gateway_route(value)
    path = root / "data/materials/.technical-host/gateway-route.json"
    if path.resolve() != path:
        raise ValueError("Gateway route cannot use links")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    if temporary.is_symlink():
        raise ValueError("Gateway route cannot use links")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def deployment(root):
    marker = root / ".updates/active.json"
    if not marker.exists():
        state_path = root.parent / "state.json"
        recovered = root.parent / "compose.json"
        if root.name == "workspace" and state_path.is_file() and recovered.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                re.fullmatch(
                    r"dispatcher_recovery_[a-f0-9]{12}", state.get("project", "")
                )
                and state.get("status") in {"PREPARED", "ACTIVE"}
                and recovered.resolve() == recovered
            ):
                return recovered, state["project"]
        return None
    key = (root / ".secrets/backup-signing.key").read_bytes().strip()
    value = read_signed(marker, key, PURPOSE)
    relative = value.get("compose", "")
    if (
        value.get("root") != str(root)
        or not re.fullmatch(r"install-[a-f0-9]{32}/(?:old|new)-compose.json", relative)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value.get("project", ""))
    ):
        raise ValueError("Invalid installed deployment")
    path = root / ".updates" / relative
    if (
        path.resolve() != path
        or path.stat().st_size > 4 * 1024 * 1024
        or hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]
    ):
        raise ValueError("The installed deployment has changed")
    return path, value["project"]


def base_files(root):
    """Keep the installation's selected TLS mode when adding runtime overrides."""
    values = {}
    environment = root / ".env"
    if environment.is_file():
        for line in environment.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() in {"COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR"}:
                values[name.strip()] = value.strip().strip("\"'")
    for name in ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR"):
        if name in os.environ:
            values[name] = os.environ[name]
    selected = values.get("COMPOSE_FILE")
    if selected:
        return [
            root / name
            for name in selected.split(values.get("COMPOSE_PATH_SEPARATOR", os.pathsep))
        ]
    paths = [root / "docker-compose.yml"]
    override = root / "docker-compose.override.yml"
    if override.is_file():
        paths.append(override)
    return paths


def command(root, arguments):
    if offline_images(root):
        if arguments and arguments[0] == "build":
            raise ValueError(
                "Offline installation uses prepared images; rebuild on the preparation host"
            )
        arguments = [
            "--no-build" if value == "--build" else value for value in arguments
        ]
    selected = deployment(root)
    overlay = resource_overlay(root)
    if selected is None:
        base = ["docker", "compose", "--project-directory", str(root)]
        if overlay:
            for path in [*base_files(root), overlay]:
                base += ["-f", str(path)]
        return [*base, *arguments]
    if any(
        arg in {"-f", "--file", "-p", "--project-name", "--project-directory"}
        or arg.startswith(("--file=", "--project-name=", "--project-directory="))
        for arg in arguments
    ):
        raise ValueError("Use the signed deployment without project or file overrides")
    path, project = selected
    return [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "-p",
        project,
        "-f",
        str(path),
        *(["-f", str(overlay)] if overlay else []),
        *arguments,
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--installation", type=Path, default=ROOT)
    options, arguments = parser.parse_known_args()
    root = options.installation.resolve()
    if arguments and arguments[0] == "down":
        import service_monitor

        service_monitor.stop(root)
    code = subprocess.run(command(root, arguments), cwd=root).returncode
    if (
        not code
        and arguments
        and arguments[0] == "up"
        and any(flag in arguments for flag in ("--detach", "-d"))
    ):
        import service_monitor

        service_monitor.start(root)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Compose startup refused: {error}") from None
