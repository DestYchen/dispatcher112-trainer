import subprocess

import pytest

import install_offline as install


@pytest.mark.parametrize("project", ["", "../existing", "UPPER", "x" * 64, "a\n-p victim"])
def test_invalid_project_cannot_reach_docker(project):
    with pytest.raises(ValueError, match="project"):
        install.installation_options(project, 25173, 28000, "127.0.0.3")


@pytest.mark.parametrize("ui,api", [(80, 8000), (5173, 65536), (5173, 5173)])
def test_ports_must_be_distinct_and_unprivileged(ui, api):
    with pytest.raises(ValueError, match="ports"):
        install.installation_options("acceptance", ui, api, "127.0.0.3")


@pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.10", "::1", "localhost"])
def test_installer_cannot_expose_training_media_externally(address):
    with pytest.raises(ValueError):
        install.installation_options("acceptance", 25173, 28000, address)


@pytest.mark.parametrize("occupied", ["ps", "volume", "network"])
def test_existing_installation_resources_are_preserved(monkeypatch, occupied):
    commands = []

    def inspect(command):
        commands.append(command)
        assert command[-1] == "label=com.docker.compose.project=acceptance"
        return b"existing-resource\n" if command[1] == occupied else b""

    monkeypatch.setattr(subprocess, "check_output", inspect)
    with pytest.raises(ValueError, match="already has data"):
        install.check_unused("acceptance", 25173, 28000, "127.0.0.3")
    assert commands[-1][1] == occupied


def test_separate_installation_uses_own_ports_and_origins():
    route = install.installation_options("acceptance", 25173, 28000, "127.0.0.3")
    assert {p["published"] for p in route["ports"]} == {"25173", "28000"}
    assert {p["host_ip"] for p in route["ports"]} == {"127.0.0.1"}
    assert route["cors_origins"] == ["https://localhost:25173", "https://127.0.0.1:25173"]
