"""Build a separate local recovery deployment from the archived Compose definition."""

import copy
import ipaddress
import re
from pathlib import Path
from typing import Any

PROJECT = re.compile(r"dispatcher_recovery_[a-f0-9]{12}")
SERVICES = frozenset(
    {
        "prepare_runtime",
        "migrate",
        "checks",
        "frontend_checks",
        "backend",
        "worker",
        "sip_worker",
        "frontend",
        "gateway",
        "postgres",
        "redis",
        "languagetool",
        "languagetool_tls",
        "ollama",
        "ollama_tls",
        "backup",
        "telephony",
    }
)


def recovery_compose(
    source: dict[str, Any],
    workspace: Path,
    project: str,
    images: dict[str, str],
    ui_port: int,
    api_port: int,
    media_address: str,
) -> dict[str, Any]:
    if not PROJECT.fullmatch(project):
        raise ValueError("Invalid recovery project name")
    if ui_port == api_port or not all(1024 <= port <= 65535 for port in (ui_port, api_port)):
        raise ValueError("Recovery needs two different unprivileged ports")
    address = ipaddress.ip_address(media_address)
    if address.version != 4 or not address.is_loopback or str(address) == "127.0.0.1":
        raise ValueError("Use a separate loopback IPv4 address for recovery media")
    value = copy.deepcopy(source)
    value["name"] = project
    value["services"].pop("control", None)
    if set(value["services"]) != SERVICES:
        raise ValueError("Unsupported service composition; restore with the matching installer")
    for name, definition in value.get("volumes", {}).items():
        if definition.get("external") or definition.get("driver_opts"):
            raise ValueError("External volumes are not allowed in the archived deployment")
        definition["name"] = f"{project}_{name}"
    for name, definition in value.get("networks", {}).items():
        if definition.get("external"):
            raise ValueError("External networks are not allowed in recovery")
        definition["name"] = f"{project}_{name}"
    value["volumes"]["recovery_files"] = {"name": f"{project}_files", "external": True}
    value["volumes"]["recovery_backups"] = {"name": f"{project}_backups"}
    models = workspace / "data/llm"
    for name, service in value["services"].items():
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", images.get(name, "")):
            raise ValueError(f"Missing installed image for {name}")
        if (
            service.get("privileged")
            or service.get("pid") == "host"
            or service.get("network_mode") == "host"
        ):
            raise ValueError("Host privileges are not allowed in a recovery deployment")
        service.pop("build", None)
        service.pop("container_name", None)
        service["image"] = images[name]
        service["pull_policy"] = "never"
        mounts = []
        for mount in service.get("volumes", []):
            if mount["type"] != "bind":
                if mount.get("source") not in value["volumes"]:
                    raise ValueError("Unknown named recovery volume")
                mounts.append(mount)
                continue
            path = Path(mount["source"])
            if not path.is_relative_to(workspace) or ".." in path.parts:
                raise ValueError("Archived bind mount escapes the recovered workspace")
            if path == workspace / ".backups":
                mounts.append(
                    {"type": "volume", "source": "recovery_backups", "target": mount["target"]}
                )
            elif path.is_relative_to(models):
                relative = path.relative_to(workspace).as_posix()
                mounts.append(
                    {
                        "type": "volume",
                        "source": "recovery_files",
                        "target": mount["target"],
                        "read_only": True,
                        "volume": {"nocopy": True, "subpath": f"workspace/{relative}"},
                    }
                )
            else:
                mount["bind"] = {"create_host_path": False}
                mounts.append(mount)
                if models.is_relative_to(path):
                    mounts.append(
                        {
                            "type": "volume",
                            "source": "recovery_files",
                            "target": mount["target"].rstrip("/")
                            + "/"
                            + models.relative_to(path).as_posix(),
                            "read_only": True,
                            "volume": {"nocopy": True, "subpath": "workspace/data/llm"},
                        }
                    )
        service["volumes"] = mounts
        if name in {"backend", "worker", "sip_worker", "checks"}:
            service["environment"]["CORS_ORIGINS"] = (
                f'["https://localhost:{ui_port}","https://127.0.0.1:{ui_port}"]'
            )
    value["services"]["gateway"]["ports"] = [
        {"target": 5173, "published": str(ui_port), "host_ip": "127.0.0.1", "protocol": "tcp"},
        {"target": 8000, "published": str(api_port), "host_ip": "127.0.0.1", "protocol": "tcp"},
    ]
    telephony = value["services"]["telephony"]
    telephony["environment"]["SIP_MEDIA_ADDRESS"] = str(address)
    telephony["ports"] = [
        {"target": port, "published": str(port), "host_ip": str(address), "protocol": "udp"}
        for port in range(10000, 10200)
    ]
    value["services"]["postgres"]["volumes"].append(
        {
            "type": "volume",
            "source": "recovery_files",
            "target": "/recovery-database",
            "read_only": True,
            "volume": {"nocopy": True, "subpath": "snapshot/database"},
        }
    )
    return value
