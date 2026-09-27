"""Exercise backups, repeat activation and restart of an isolated recovered instance."""

import argparse
import hashlib
import json
import ssl
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

import recover_snapshot as recovery
from check_recovery_live import postgres


def api(client, path, body=None):
    response = client.get(path) if body is None else client.post(path, json=body)
    response.raise_for_status()
    return response.json()


def login(base, workspace, name, password):
    client = httpx.Client(
        base_url=base + "/api/v1",
        verify=ssl.create_default_context(cafile=workspace / ".secrets/pki/ca.crt"),
        trust_env=False,
        timeout=30,
        headers={"Origin": base},
    )
    preauth = client.get("/auth/me")
    assert preauth.status_code == 401
    client.headers["X-CSRF-Token"] = preauth.json()["error"]["details"]["csrf_token"]
    signed_in = api(client, "/auth/login", {"login": name, "password": password})
    client.headers["X-CSRF-Token"] = signed_in["csrf_token"]
    return client


def job(client, kind, **fields):
    identity = str(uuid4())
    api(
        client,
        "/admin/operations/jobs",
        {"id": identity, "kind": kind, "service": "backup", **fields},
    )
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        state = api(client, "/admin/operations/jobs/" + identity)
        if state["status"] in {"SUCCEEDED", "FAILED"}:
            assert state["status"] == "SUCCEEDED", state
            return state
        time.sleep(1)
    raise AssertionError("Recovery backup operation exceeded 240 seconds")


def data_digest(target):
    pairs = []
    for table in (
        "users",
        "lessons",
        "assignments",
        "status_events",
        "phone_reports",
        "learning_materials",
    ):
        pairs.append(
            f"'{table}', (SELECT json_build_array(count(*), "
            f"md5(string_agg(row_to_json(t)::text,'' ORDER BY id))) FROM {table} t)"
        )
    value, _ = postgres(target, "SELECT json_build_object(" + ",".join(pairs) + ")")
    return json.loads(value)


def recording_digest(client, identity):
    response = client.get(f"/sip/calls/{identity}/recording")
    response.raise_for_status()
    assert response.content[:4] == b"RIFF"
    return hashlib.sha256(response.content).hexdigest()


def check(target, call_id):
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "ACTIVE" and not state.get("stopped")
    assert state.get("backup_schedule_restored")
    workspace = target / "workspace"
    base = f"https://localhost:{state['ui_port']}"
    result = {
        "passed": False,
        "project": state["project"],
        "snapshot": state["snapshot"],
        "base_url": base,
    }
    fixture = json.loads(
        (workspace / "data/sip-acceptance.json").read_text(encoding="utf-8")
    )
    admin = login(base, workspace, "admin", "admin")
    teacher = login(base, workspace, fixture["teacher"], "teacher")
    original_schedule = None
    maintenance_enabled = False
    try:
        maintenance = api(admin, "/admin/maintenance")
        assert not maintenance["enabled"] and not maintenance.get("job_id")
        original_schedule = api(admin, "/admin/backups")["schedule"]
        expected_schedule = json.loads(
            (target / "extraction.json").read_text(encoding="utf-8")
        )["original_schedule"]
        assert original_schedule == expected_schedule
        api(
            admin,
            "/admin/maintenance",
            {"enabled": True, "reason": "Recovery persistence acceptance"},
        )
        maintenance_enabled = True
        changed_schedule = {
            **original_schedule,
            "enabled": not original_schedule["enabled"],
        }
        job(admin, "backup_configure", schedule=changed_schedule)
        recovery.run(
            sys.executable,
            str(recovery.ROOT / "scripts/recover_snapshot.py"),
            "activate",
            "--target",
            str(target),
        )
        assert api(admin, "/admin/backups")["schedule"] == changed_schedule
        assert api(admin, "/admin/maintenance")["enabled"]
        activated, _ = postgres(
            target,
            "SELECT count(*) FROM audit_log WHERE action='SYSTEM_RECOVERY_ACTIVATED'",
        )
        assert activated == "1"
        result["repeat_activation_preserves_operator_schedule_and_maintenance"] = True
        print(
            "PASS: repeat activation preserves operator schedule and maintenance",
            flush=True,
        )

        created = job(admin, "backup_create")
        assert created["result"]["schema"] == "dispatcher-backup-2"
        assert created["result"]["program_files"] >= 300
        verified = job(admin, "backup_verify", snapshot=created["result"]["name"])
        assert (
            verified["result"]["source_unchanged"]
            and verified["result"]["audit_triggers"] >= 1
        )
        assert verified["result"]["database_role"] == "dispatcher_backup"
        result["created_backup"] = created
        result["verified_backup"] = verified
        job(admin, "backup_configure", schedule=original_schedule)
        print(
            "PASS: recovered installation creates and verifies its own complete backup",
            flush=True,
        )

        before = data_digest(target)
        audit_max, _ = postgres(target, "SELECT max(id) FROM audit_log")
        query = (
            "SELECT count(*),md5(string_agg(row_to_json(a)::text,'' ORDER BY id)) FROM audit_log a WHERE id<="
            + audit_max
        )
        audit_before, _ = postgres(target, query)
        recording_before = recording_digest(teacher, call_id)
        for action in ("stop", "start"):
            recovery.run(
                sys.executable,
                str(recovery.ROOT / "scripts/recover_snapshot.py"),
                action,
                "--target",
                str(target),
            )
        assert data_digest(target) == before
        audit_after, _ = postgres(target, query)
        assert audit_after == audit_before
        assert api(admin, "/auth/me")["role"] == "ADMIN"
        assert recording_digest(teacher, call_id) == recording_before
        assert api(admin, "/admin/backups")["schedule"] == original_schedule
        result["restart"] = {
            "tables": before,
            "audit_max": int(audit_max),
            "audit_digest": audit_before,
            "recording_sha256": recording_before,
            "session_preserved": True,
        }
        print(
            "PASS: restart preserves users, lessons, cards, reports, materials, audit, recording and session",
            flush=True,
        )

        source_query = (
            "SELECT count(*),md5(string_agg(row_to_json(a)::text,'' ORDER BY id)) FROM audit_log a WHERE id<="
            + str(state["database"]["source_audit_max"])
        )
        restored, source = postgres(target, source_query)
        assert restored == source
        assert int(restored.split("|")[0]) == state["database"]["source_audit_count"]
        result["source_audit_unchanged"] = True
        result["passed"] = True
    finally:
        try:
            if maintenance_enabled:
                maintenance = api(admin, "/admin/maintenance")
                if not maintenance.get("job_id"):
                    if (
                        original_schedule
                        and api(admin, "/admin/backups")["schedule"]
                        != original_schedule
                    ):
                        job(admin, "backup_configure", schedule=original_schedule)
                    api(
                        admin,
                        "/admin/maintenance",
                        {
                            "enabled": False,
                            "reason": "Recovery persistence acceptance finished",
                        },
                    )
        finally:
            admin.close()
            teacher.close()
            result["checked_at"] = datetime.now(UTC).isoformat()
            (
                recovery.ROOT / "artifacts/ui-review/recovery-persistence.json"
            ).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--call-id", required=True)
    arguments = parser.parse_args()
    check(recovery.checked_target(arguments.target), arguments.call_id)


if __name__ == "__main__":
    main()
