"""Keep installation resources and privileges stable while changing a signed software release."""

import copy
import re
from pathlib import Path
from typing import Any

from app.operations.recovery_compose import SERVICES

RESOURCE_FIELDS = (
    "container_name",
    "ports",
    "volumes",
    "networks",
    "user",
    "cap_add",
    "cap_drop",
    "read_only",
    "security_opt",
    "privileged",
    "pid",
    "ipc",
    "network_mode",
    "devices",
    "tmpfs",
    "profiles",
)


def pin_deployment(value: dict[str, Any], images: dict[str, str]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["services"].pop("control", None)
    if set(result["services"]) != SERVICES or set(images) != SERVICES:
        raise ValueError("Unsupported service composition for a software update")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", result["name"]):
        raise ValueError("Invalid installed project name")
    for name, service in result["services"].items():
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", images[name]):
            raise ValueError("Software update images must be pinned to local SHA-256 IDs")
        service.pop("build", None)
        service["image"] = images[name]
        service["pull_policy"] = "never"
    return result


def update_deployment(
    previous: dict[str, Any], candidate: dict[str, Any], images: dict[str, str], root: Path
) -> dict[str, Any]:
    result = pin_deployment(candidate, images)
    before = copy.deepcopy(previous)
    before["services"].pop("control", None)
    if set(before["services"]) != SERVICES:
        raise ValueError("Unsupported installed service composition")
    for key in ("name", "networks", "volumes", "secrets", "configs"):
        if before.get(key) != result.get(key):
            raise ValueError("A software update cannot relocate installation resources")
    for name, service in result["services"].items():
        old = before["services"][name]
        if service.get("post_start") or service.get("pre_stop"):
            raise ValueError("Software updates cannot add lifecycle hooks")
        for field in RESOURCE_FIELDS:
            if service.get(field) != old.get(field):
                raise ValueError(f"Update changes installed {name} resource or privilege: {field}")
        for key, value in old.get("environment", {}).items():
            if service.get("environment", {}).get(key) != value:
                raise ValueError(f"Update changes an existing {name} installation setting")
        if (
            service.get("privileged")
            or service.get("pid") == "host"
            or service.get("network_mode") == "host"
        ):
            raise ValueError("Software updates cannot use host privileges")
        for mount in service.get("volumes", []):
            if mount["type"] == "bind":
                source = Path(mount["source"])
                if (
                    not source.is_absolute()
                    or not source.is_relative_to(root)
                    or ".." in source.parts
                ):
                    raise ValueError("A bind mount escapes the selected installation")
    return result
