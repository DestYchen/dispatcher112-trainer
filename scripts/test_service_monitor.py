"""Monitor ownership and shutdown never rely on terminating a stored process ID."""

from datetime import UTC, datetime, timedelta

import service_monitor as monitor


def test_restart_reuses_a_live_monitor_and_stop_revokes_ownership(
    tmp_path, monkeypatch
):
    launched = []
    monkeypatch.setattr(
        monitor.subprocess,
        "Popen",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    assert monitor.start(tmp_path)["status"] == "starting"
    token = monitor.read(tmp_path, "monitor.json")["token"]
    monitor.heartbeat(tmp_path, token, {"services": 13})
    assert monitor.start(tmp_path)["status"] == "running"
    assert len(launched) == 1
    assert monitor.stop(tmp_path)["status"] == "stopped"
    assert not monitor.active(tmp_path, token)
    before = monitor.read(tmp_path, "monitor-status.json")
    monitor.heartbeat(tmp_path, token, {"services": 0})
    assert monitor.read(tmp_path, "monitor-status.json") == before


def test_an_old_process_cannot_claim_a_new_monitors_result(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda *args, **kwargs: None)
    monitor.start(tmp_path)
    old = monitor.read(tmp_path, "monitor.json")["token"]
    monitor.stop(tmp_path)
    monitor.start(tmp_path)
    assert not monitor.active(tmp_path, old)
    monitor.heartbeat(tmp_path, old, {"services": 13})
    assert monitor.status(tmp_path)["status"] == "starting"


def test_missing_or_failed_samples_are_not_reported_as_running(tmp_path):
    stamp = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    monitor.write(
        tmp_path,
        "monitor.json",
        {"enabled": True, "token": "owned", "started_at": stamp},
    )
    monitor.write(
        tmp_path, "monitor-status.json", {"token": "owned", "at": stamp, "services": 13}
    )
    assert monitor.status(tmp_path)["status"] == "unavailable"
    monitor.heartbeat(
        tmp_path, "owned", {"status": "unavailable", "error": "RuntimeError"}
    )
    assert monitor.status(tmp_path)["status"] == "unavailable"


def test_process_launch_failure_leaves_monitor_stopped(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("Cannot start Python")

    monkeypatch.setattr(monitor.subprocess, "Popen", failed)
    import pytest

    with pytest.raises(OSError):
        monitor.start(tmp_path)
    assert monitor.status(tmp_path)["status"] == "stopped"
