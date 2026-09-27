"""Explicit UI fixtures for all backup states; never runs actual backup jobs."""

import json
from datetime import UTC, datetime
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, browser_args
from check_responsive import OUTPUT, layouts


def main():
    result = {"passed": False, "mode": "UI fixtures, no backup operations", "layouts": [], "errors": []}
    state = {"mode": "loading", "enabled": False, "post_error": False, "worker": True,
             "schedule": {"enabled": True, "hour": 3, "minute": 0, "retention": 14}, "jobs": {}}
    snapshot = {"name": "backup-20260926T080000Z-123456789abc", "created_at": "2026-09-26T08:00:00+00:00",
                "files": 530, "bytes": 1954669316, "program_files": 360,
                "valid_manifest": True, "verification": None}
    legacy = {**snapshot, "name": "backup-20260925T080000Z-123456789abc", "program_files": 0}
    held = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: result["errors"].append(str(error)))

        def reply(route):
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            status = 200
            if path == "/auth/me":
                value = {"id": "backup-fixture-admin", "login": "fixture", "full_name": "Администратор проверки",
                         "short_name": "Проверка", "role": "ADMIN", "service": None, "csrf_token": "fixture",
                         "server_time": datetime.now(UTC).isoformat()}
            elif path == "/admin/maintenance":
                if route.request.method == "POST":
                    state["enabled"] = route.request.post_data_json["enabled"]
                value = {"enabled": state["enabled"], "reason": "Проверка интерфейса"}
            elif path == "/admin/backups":
                if state["mode"] == "loading":
                    held.append(route)
                    return
                if state["mode"] == "error":
                    status, value = 503, {"error": {"code": "BACKUP_UNAVAILABLE", "message": "Проверка ошибки резервирования."}}
                else:
                    value = {"items": [] if state["mode"] == "empty" else [snapshot, legacy],
                             "schedule": state["schedule"], "worker_ready": state["worker"]}
            elif path == "/admin/operations/jobs":
                if state["post_error"]:
                    status, value = 503, {"error": {"code": "BACKUP_UNAVAILABLE", "message": "Проверка ошибки сохранения."}}
                else:
                    body = route.request.post_data_json
                    if body["kind"] == "backup_configure":
                        state["schedule"] = body["schedule"]
                    outcome = {"schedule": state["schedule"]} if body["kind"] == "backup_configure" else {
                        "snapshot": snapshot["name"], "files": 166, "restored_users": 470, "restored_types": 1200,
                        "restored_audit": 28389, "source_unchanged": True, "program_files": 360,
                    }
                    state["jobs"][body["id"]] = {"id": body["id"], "status": "SUCCEEDED", "result": outcome}
                    value, status = state["jobs"][body["id"]], 202
            elif path.startswith("/admin/operations/jobs/"):
                value = state["jobs"][path.rsplit("/", 1)[1]]
            else:
                value = {"items": [], "next_cursor": None}
            route.fulfill(status=status, json=value)

        page.route("**/api/v1/**", reply)
        try:
            page.goto(BASE_URL + "/admin")
            page.get_by_role("button", name="Резервные копии", exact=True).click()
            expect(page.get_by_text("Загрузка…", exact=True)).to_be_visible()
            assert held
            state["mode"] = "ready"
            for route in held:
                reply(route)
            create = page.get_by_role("button", name="Создать полную копию", exact=True)
            expect(create).to_be_disabled()
            expect(page.get_by_text("Файлов программы: 360", exact=True)).to_be_visible()
            expect(page.get_by_text("Старая копия без исходного кода. Для восстановления нужен соответствующий комплект программы.", exact=True)).to_be_visible()
            layouts(page, "backup-ready-fixture", result["layouts"])
            page.get_by_label("Причина обслуживания", exact=True).fill("Проверка интерфейса копий")
            page.get_by_role("button", name="Включить обслуживание", exact=True).click()
            expect(create).to_be_enabled()
            retention = page.get_by_label("Число сохраняемых полных копий", exact=True)
            retention.fill("18")
            state["post_error"] = True
            page.get_by_role("button", name="Сохранить расписание", exact=True).click()
            expect(page.get_by_text("Проверка ошибки сохранения.", exact=True)).to_be_visible()
            expect(retention).to_have_value("18")
            state["post_error"] = False
            state["mode"] = "error"
            expect(page.get_by_text("Проверка ошибки резервирования.", exact=True)).to_be_visible()
            expect(retention).to_have_value("18")
            expect(page.get_by_role("button", name="Сохранить расписание", exact=True)).to_be_disabled()
            layouts(page, "backup-refresh-error-fixture", result["layouts"])
            state["mode"] = "ready"
            page.get_by_role("button", name="Обновить список копий", exact=True).first.click()
            expect(page.get_by_role("button", name="Сохранить расписание", exact=True)).to_be_enabled()
            page.get_by_role("button", name="Сохранить расписание", exact=True).click()
            expect(page.get_by_text("Расписание резервирования сохранено.", exact=True)).to_be_visible()
            page.get_by_role("button", name="Проверить восстановление", exact=True).first.click()
            expect(page.get_by_text("Восстановление проверено в отдельной базе. Рабочая база сохранена.", exact=True)).to_be_visible()
            layouts(page, "backup-result-fixture", result["layouts"])
            state["mode"] = "empty"
            page.get_by_role("button", name="Обновить список копий", exact=True).click()
            expect(page.get_by_text("Полных резервных копий пока нет. Включите обслуживание и создайте первую копию.", exact=True)).to_be_visible()
            layouts(page, "backup-empty-fixture", result["layouts"])
            state["mode"] = "error"
            page.reload()
            page.get_by_role("button", name="Резервные копии", exact=True).click()
            expect(page.get_by_text("Проверка ошибки резервирования.", exact=True)).to_be_visible()
            layouts(page, "backup-initial-error-fixture", result["layouts"])
            assert not result["errors"], result["errors"]
            result["passed"] = True
            print("PASS: backup UI fixtures, loading/empty/error/ready, preserved inputs, 35 layouts", flush=True)
        finally:
            (OUTPUT / "backup-ui-fixtures.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()


if __name__ == "__main__":
    main()
