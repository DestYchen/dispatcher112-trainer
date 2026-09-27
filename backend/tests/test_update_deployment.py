import copy
from pathlib import Path

import pytest

from app.operations.recovery_compose import SERVICES
from app.operations.update_deployment import pin_deployment, update_deployment
from tests.test_recovery import composition


def test_update_pins_images_preserves_resources_and_accepts_new_program_command(
    tmp_path: Path,
) -> None:
    previous = composition(tmp_path)
    candidate = copy.deepcopy(previous)
    candidate["services"]["backend"]["command"] = ["python", "-m", "app.main"]
    images = dict.fromkeys(SERVICES, "sha256:" + "a" * 64)
    result = update_deployment(previous, candidate, images, tmp_path)
    assert "control" not in result["services"]
    assert result["services"]["backend"]["command"] == ["python", "-m", "app.main"]
    assert result["volumes"] == previous["volumes"]
    assert "build" in candidate["services"]["backend"]
    for service in result["services"].values():
        assert service["image"] == images["backend"] and service["pull_policy"] == "never"
        assert "build" not in service


@pytest.mark.parametrize(
    "field,value",
    [
        ("privileged", True),
        ("pid", "host"),
        ("network_mode", "host"),
        ("user", "0:0"),
        ("cap_add", ["SYS_ADMIN"]),
        ("read_only", False),
        ("security_opt", []),
        ("ports", [{"published": "80", "target": 8000}]),
        ("volumes", [{"type": "bind", "source": "/var/run/docker.sock", "target": "/socket"}]),
        ("networks", {"unrestricted": {}}),
        ("profiles", ["different"]),
        ("post_start", [{"command": "unexpected hook"}]),
        ("pre_stop", [{"command": "hook"}]),
    ],
)
def test_update_cannot_change_privileges_or_physical_resources(
    tmp_path: Path, field: str, value: object
) -> None:
    before = composition(tmp_path)
    after = copy.deepcopy(before)
    after["services"]["backend"][field] = value
    with pytest.raises(ValueError):
        update_deployment(before, after, dict.fromkeys(SERVICES, "sha256:" + "a" * 64), tmp_path)


@pytest.mark.parametrize(
    "changed", ["name", "volumes", "networks", "environment", "unknown", "tag"]
)
def test_update_refuses_changed_database_settings_topology_or_unpinned_images(
    tmp_path: Path, changed: str
) -> None:
    before = composition(tmp_path)
    before["services"]["backend"]["environment"] = {"DATABASE_URL": "installed-value"}
    after = copy.deepcopy(before)
    images = dict.fromkeys(SERVICES, "sha256:" + "a" * 64)
    if changed in ("volumes", "networks"):
        after[changed] = {}
    elif changed == "name":
        after["name"] = "another_project"
    elif changed == "environment":
        after["services"]["backend"]["environment"]["DATABASE_URL"] = "different-database"
    elif changed == "unknown":
        after["services"]["unreviewed"] = {"image": "arbitrary"}
    else:
        images["backend"] = "backend:latest"
    with pytest.raises(ValueError):
        update_deployment(before, after, images, tmp_path)


def test_installation_itself_cannot_hide_an_external_bind_mount(tmp_path: Path) -> None:
    before = composition(tmp_path)
    before["services"]["backend"]["volumes"][0]["source"] = str(tmp_path.parent / "outside")
    with pytest.raises(ValueError, match="escapes"):
        update_deployment(before, before, dict.fromkeys(SERVICES, "sha256:" + "a" * 64), tmp_path)
    with pytest.raises(ValueError):
        pin_deployment(before, {})
