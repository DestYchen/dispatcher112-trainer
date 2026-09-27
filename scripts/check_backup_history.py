"""Verify two existing full snapshots and their independent, durable UI results."""

import json

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, browser_args
from check_backup_live import wait_job
from check_responsive import OUTPUT, api, layouts, login


def main():
    result = {"passed": False, "mode": "real isolated recovery", "jobs": [], "layouts": [], "errors": []}
    enabled = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        page.set_default_timeout(15000)
        try:
            login(page, "admin", "admin")
            state = api(page, "/admin/maintenance")
            assert not state["enabled"] and not state.get("job_id"), "Preserve existing maintenance"
            catalog = api(page, "/admin/backups")
            names = [row["name"] for row in catalog["items"] if row["valid_manifest"]][:2]
            assert catalog["worker_ready"] and len(names) == 2, "Two valid existing snapshots required"
            page.get_by_role("button", name="Резервные копии", exact=True).click()
            page.get_by_label("Причина обслуживания", exact=True).fill("Проверка истории восстановления копий")
            page.get_by_role("button", name="Включить обслуживание", exact=True).click()
            expect(page.get_by_text("Режим обслуживания включён", exact=True)).to_be_visible()
            enabled = True
            verified = {}
            for name in names:
                article = page.locator("article").filter(has_text=name).first
                with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/admin/operations/jobs")) as submitted:
                    article.get_by_role("button", name="Проверить восстановление", exact=True).click()
                job = wait_job(page, submitted.value.json()["id"])
                assert job["result"]["snapshot"] == name
                assert job["result"]["source_unchanged"] and job["result"]["audit_triggers"] >= 1
                verified[name] = job["result"]
                result["jobs"].append(job)
                # A fresh document must read saved history, without relying on the last job UI.
                page.goto(BASE_URL + "/admin")
                page.get_by_role("button", name="Резервные копии", exact=True).click()
                current = {row["name"]: row for row in api(page, "/admin/backups")["items"]}
                for snapshot, outcome in verified.items():
                    assert current[snapshot]["verification"] == outcome
                    article = page.locator("article").filter(has_text=snapshot).first
                    expect(article.get_by_text("Восстановление проверено в отдельной базе. Рабочая база сохранена.", exact=True)).to_be_visible()
                print(f"PASS: {len(verified)} independent verification results persisted after reload", flush=True)
            layouts(page, "backup-history-live", result["layouts"])
            assert not result["errors"], result["errors"]
            result["passed"] = True
        except Exception as error:
            result["failure"] = str(error)
            raise
        finally:
            try:
                if enabled:
                    state = api(page, "/admin/maintenance")
                    if state.get("job_id"):
                        wait_job(page, state["job_id"])
                    api(page, "/admin/maintenance", "POST", {"enabled": False, "reason": "Проверка истории восстановления завершена"})
            except Exception as error:
                result["passed"] = False
                result["cleanup_error"] = str(error)
                raise
            finally:
                (OUTPUT / "backup-history-live.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                browser.close()


if __name__ == "__main__":
    main()
