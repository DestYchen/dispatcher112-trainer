"""Exercise a real, prepared recovery installation without replacing the source."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import recover_snapshot as recovery


def postgres(target, sql):
    deployment = json.loads((target / "compose.json").read_text(encoding="utf-8"))
    environment = deployment["services"]["postgres"]["environment"]
    arguments = (
        "exec",
        "-T",
        "postgres",
        "psql",
        "--username",
        environment["POSTGRES_USER"],
        "--dbname",
        environment["POSTGRES_DB"],
        "--tuples-only",
        "--no-align",
        "--command",
        sql,
    )
    return recovery.compose(target, *arguments), recovery.run(
        "docker", "compose", *arguments
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    arguments = parser.parse_args()
    target = recovery.checked_target(arguments.target)
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "PREPARED", "Use a fresh prepared recovery"
    workspace = target / "workspace"
    output = recovery.ROOT / "artifacts/ui-review"
    os.environ["DISPATCHER_UI_URL"] = f"https://localhost:{state['ui_port']}"
    import browser_environment

    browser_environment.ROOT = workspace
    os.environ["NODE_EXTRA_CA_CERTS"] = str(workspace / ".secrets/pki/ca.crt")
    from check_responsive import api, layouts, login
    from playwright.sync_api import expect, sync_playwright

    result = {
        "passed": False,
        "project": state["project"],
        "snapshot": state["snapshot"],
        "base_url": browser_environment.BASE_URL,
        "started_at": datetime.now(UTC).isoformat(),
        "layouts": [],
        "errors": [],
    }
    maximum = int(state["database"]["source_audit_max"])
    query = (
        "SELECT count(*),md5(string_agg(row_to_json(a)::text,'' ORDER BY id)) "
        f"FROM audit_log a WHERE id<={maximum}"
    )
    restored, original = postgres(target, query)
    assert restored == original
    count, digest = restored.split("|")
    assert int(count) == state["database"]["source_audit_count"]
    result["audit"] = {
        "rows": int(count),
        "maximum_id": maximum,
        "same_content_digest": digest,
    }
    print(
        f"PASS: all {count} original audit rows are unchanged in the recovered database",
        flush=True,
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=browser_environment.browser_args())
            old_context = browser.new_context()
            old_page = old_context.new_page()
            old_page.goto("https://localhost:5173")
            old_page.get_by_label("Логин", exact=True).fill("admin")
            old_page.get_by_label("Пароль", exact=True).fill("admin")
            old_page.get_by_role(
                "button", name="Вход в учебную систему", exact=True
            ).click()
            expect(
                old_page.get_by_role("button", name="Выйти", exact=True)
            ).to_be_visible()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_cookies(old_context.cookies())
            page = context.new_page()
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(browser_environment.BASE_URL)
            expect(page.get_by_label("Логин", exact=True)).to_be_visible()
            assert (
                page.evaluate("async () => (await fetch('/api/v1/auth/me')).status")
                == 401
            )
            result["old_sessions_rejected"] = True
            login(page, "admin", "admin")
            assert api(page, "/admin/maintenance")["enabled"]
            catalog = api(page, "/admin/backups")
            assert (
                catalog["worker_ready"]
                and not catalog["schedule"]["enabled"]
                and not catalog["items"]
            )
            csrf = api(page, "/auth/me")["csrf_token"]
            attempted = page.evaluate(
                """async csrf => {
              const r = await fetch('/api/v1/admin/maintenance', {method:'POST',
                headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
                body:JSON.stringify({enabled:false,reason:'Recovery acceptance guard'})});
              return {status:r.status,body:await r.json()};
            }""",
                csrf,
            )
            assert (
                attempted["status"] == 409
                and attempted["body"]["error"]["code"] == "RECOVERY_NOT_ACTIVATED"
            )
            report = api(page, "/admin/diagnostics/report")
            assert all(
                row["status"] == ("EMPTY" if row["code"] == "backup_catalog" else "OK")
                for row in report["integrity"]
            )
            result["standby_integrity"] = report["integrity"]
            page.get_by_role("button", name="Технический отчёт", exact=True).click()
            expect(
                page.get_by_text("Целостность БД и защита аудита", exact=True)
            ).to_be_visible()
            layouts(page, "recovery-standby", result["layouts"])
            command = [
                sys.executable,
                str(recovery.ROOT / "scripts/recover_snapshot.py"),
                "activate",
                "--target",
                str(target),
            ]
            recovery.run(*command)
            recovery.run(*command)
            assert not api(page, "/admin/maintenance")["enabled"]
            activated, _ = postgres(
                target,
                "SELECT count(*) FROM audit_log WHERE action='SYSTEM_RECOVERY_ACTIVATED'",
            )
            assert activated == "1"
            result["activation_once"] = True
            print(
                "PASS: maintenance guard, fresh sessions, five integrity checks and idempotent activation",
                flush=True,
            )
            browser.close()
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            import check_learning

            check_learning.main()
            learning = json.loads(
                (output / "learning-browser.json").read_text(encoding="utf-8")
            )
            result["learning"] = {
                "passed": learning["passed"],
                "layouts": len(learning["layouts"]),
            }
            (output / "recovery-learning-browser.json").write_text(
                json.dumps(
                    {
                        **learning,
                        "project": state["project"],
                        "base_url": browser_environment.BASE_URL,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            import check_sip_outgoing

            check_sip_outgoing.main()
            sip = json.loads((output / "sip-outgoing.json").read_text(encoding="utf-8"))
            (output / "recovery-sip.json").write_text(
                json.dumps(
                    {
                        **sip,
                        "project": state["project"],
                        "base_url": browser_environment.BASE_URL,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            result["sip_and_status_cycle"] = True
        finally:
            os.chdir(previous)
        from check_recovery_persistence import check

        persistence = check(target, sip["call_id"])
        assert persistence["passed"]
        result["persistence_and_backup"] = True
        after, source_after = postgres(target, query)
        assert after == original == source_after
        result["source_audit_unchanged"] = True
        assert not result["errors"]
        result["passed"] = True
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        (output / "recovery-live.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        "PASS: independently restored application, files, database, learning, SIP and original audit",
        flush=True,
    )


if __name__ == "__main__":
    main()
