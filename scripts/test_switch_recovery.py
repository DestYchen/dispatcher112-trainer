import json
from contextlib import nullcontext
from uuid import uuid4

import pytest

import switch_recovery as switch


def test_fence_retries_transient_lock_without_changing_request(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs["incoming"])
        if len(calls) < 3:
            raise RuntimeError("busy")
        return '{"phase":"FENCED"}'

    monkeypatch.setattr(switch.compose, "command", lambda *args: ["docker"])
    monkeypatch.setattr(switch.technical, "run", run)
    monkeypatch.setattr(switch.time, "sleep", lambda seconds: None)
    state = {"id": str(uuid4()), "actor": str(uuid4()), "reason": "Acceptance"}
    assert switch.bridge(tmp_path, "fence", state) == {"phase": "FENCED"}
    assert len(calls) == 3 and len(set(calls)) == 1


@pytest.mark.parametrize(
    "action,attempts", [("fence", 3), ("import", 1), ("release", 1)]
)
def test_bridge_failure_is_bounded(monkeypatch, tmp_path, action, attempts):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("Unavailable")

    monkeypatch.setattr(switch.compose, "command", lambda *args: ["docker"])
    monkeypatch.setattr(switch.technical, "run", fail)
    monkeypatch.setattr(switch.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError):
        switch.bridge(
            tmp_path,
            action,
            {"id": str(uuid4()), "actor": str(uuid4()), "reason": "Acceptance"},
        )
    assert len(calls) == attempts


def test_new_recovery_cycle_preserves_completed_signed_history(monkeypatch, tmp_path):
    target = tmp_path / ".recovery/restored"
    workspace = target / "workspace"
    workspace.mkdir(parents=True)
    (target / "state.json").write_text(json.dumps({"status": "ACTIVE"}))
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    key = b"local-acceptance-signing-key-32-bytes"
    (secrets / "backup-signing.key").write_bytes(key)
    previous = {
        "id": str(uuid4()),
        "actor": str(uuid4()),
        "reason": "Previous cycle",
        "root": str(tmp_path),
        "workspace": str(workspace),
        "phase": "RETURNED",
    }
    switch.write_record(target / "gateway-switch.json", previous, key, switch.PURPOSE)
    monkeypatch.setattr(switch, "ROOT", tmp_path)
    monkeypatch.setattr(
        switch.install_update, "installation_lock", lambda root: nullcontext()
    )
    monkeypatch.setattr(switch, "route", lambda root: {"ports": [], "cors_origins": []})
    monkeypatch.setattr(switch, "runtime", lambda *args: "")
    monkeypatch.setattr(switch, "bridge", lambda *args, **kwargs: {})
    monkeypatch.setattr(switch, "archive", lambda *args: {"rows": []})
    monkeypatch.setattr(switch.compose, "save_gateway_route", lambda *args: None)
    monkeypatch.setattr(
        switch.sys,
        "argv",
        [
            "switch",
            "promote",
            "--target",
            str(target),
            "--actor",
            previous["actor"],
            "--reason",
            "New acceptance cycle",
        ],
    )
    switch.main()
    assert (
        switch.read_record(
            target / f"switch-{previous['id']}/completed.json", key, switch.PURPOSE
        )
        == previous
    )
    current = switch.read_record(target / "gateway-switch.json", key, switch.PURPOSE)
    assert current["id"] != previous["id"] and current["phase"] == "ACTIVE"
    switch.main()
    assert (
        switch.read_record(target / "gateway-switch.json", key, switch.PURPOSE)
        == current
    )
