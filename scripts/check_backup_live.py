"""Real admin backup, isolated recovery, scheduling and completion after closing a tab."""

import json
import time

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, browser_args
from check_responsive import OUTPUT, api, layouts, login


def wait_job(page, identity, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Deliberately avoid GET /jobs: the server must reconcile without a browser poll.
        state = api(page, "/admin/maintenance")
        if state.get("job_id") != identity:
            result = api(page, "/admin/operations/jobs/" + identity)
            assert result["status"] == "SUCCEEDED", result
            return result
        page.wait_for_timeout(1000)
    raise AssertionError("Backup job did not finish within the acceptance deadline")


def main():
    result = {
        "passed": False,
        "mode": "real backup executor and PostgreSQL",
        "layouts": [],
        "errors": [],
    }
    maintenance_enabled = False
    original = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        page.set_default_timeout(15000)
        try:
            login(page, "admin", "admin")
            initial = api(page, "/admin/maintenance")
            assert not initial["enabled"] and not initial.get("job_id"), (
                "Existing maintenance must be preserved"
            )
            catalog = api(page, "/admin/backups")
            assert catalog["worker_ready"]
            original = catalog["schedule"]
            page.get_by_role("button", name="Резервные копии", exact=True).click()
            page.get_by_label("Причина обслуживания", exact=True).fill(
                "Приёмка полного резервирования"
            )
            page.get_by_role("button", name="Включить обслуживание", exact=True).click()
            expect(
                page.get_by_text("Режим обслуживания включён", exact=True)
            ).to_be_visible()
            maintenance_enabled = True
            page.get_by_label("Число сохраняемых полных копий", exact=True).fill(
                str(original["retention"] + 1)
            )
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/admin/operations/jobs")
                )
            ) as submitted:
                page.get_by_role(
                    "button", name="Сохранить расписание", exact=True
                ).click()
            configured = wait_job(page, submitted.value.json()["id"])
            assert (
                configured["result"]["schedule"]["retention"]
                == original["retention"] + 1
            )
            print("PASS: actual schedule saved by the backup executor", flush=True)
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/admin/operations/jobs")
                )
            ) as submitted:
                page.get_by_role(
                    "button", name="Создать полную копию", exact=True
                ).click()
            identity = submitted.value.json()["id"]
            page.close()
            page = context.new_page()
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(BASE_URL + "/admin")
            created = wait_job(page, identity)
            name = created["result"]["name"]
            assert created["result"]["schema"] == "dispatcher-backup-2"
            assert created["result"]["program_files"] >= 300
            assert (
                created["result"]["files"] > 100
                and created["result"]["bytes"] > 1_000_000_000
            )
            result["creation"] = created
            print(
                "PASS: full snapshot completed after the initiating tab was closed",
                flush=True,
            )
            audit = api(page, "/admin/audit?limit=100")["items"]
            assert (
                sum(
                    row["entity_id"] == identity
                    and row["action"] == "TECHNICAL_OPERATION_FINISHED"
                    for row in audit
                )
                == 1
            )
            page.get_by_role("button", name="Резервные копии", exact=True).click()
            snapshot = page.locator("article").filter(has_text=name).first
            expect(snapshot).to_be_visible()
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/admin/operations/jobs")
                )
            ) as submitted:
                snapshot.get_by_role(
                    "button", name="Проверить восстановление", exact=True
                ).click()
            verified = wait_job(page, submitted.value.json()["id"])
            assert (
                verified["result"]["source_unchanged"]
                and verified["result"]["audit_triggers"] >= 1
            )
            assert verified["result"]["restored_types"] == 1200
            assert verified["result"]["database_role"] == "dispatcher_backup"
            assert verified["result"]["schema"] == "dispatcher-backup-2"
            assert verified["result"]["program_files"] == created["result"]["program_files"]
            result["verification"] = verified
            expect(
                page.get_by_text(
                    "Восстановление проверено в отдельной базе. Рабочая база сохранена.",
                    exact=True,
                ).first
            ).to_be_visible()
            layouts(page, "backup-live", result["layouts"])
            print(
                "PASS: actual restore into an isolated database and seven responsive layouts",
                flush=True,
            )
            assert not result["errors"], result["errors"]
            result["passed"] = True
        finally:
            if maintenance_enabled:
                state = api(page, "/admin/maintenance")
                if state.get("job_id"):
                    wait_job(page, state["job_id"])
                if original:
                    import uuid

                    identity = str(uuid.uuid4())
                    api(
                        page,
                        "/admin/operations/jobs",
                        "POST",
                        {
                            "id": identity,
                            "kind": "backup_configure",
                            "service": "backup",
                            "schedule": original,
                        },
                    )
                    wait_job(page, identity)
                api(
                    page,
                    "/admin/maintenance",
                    "POST",
                    {"enabled": False, "reason": "Приёмка резервирования завершена"},
                )
            (OUTPUT / "backup-live.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            browser.close()


if __name__ == "__main__":
    main()
