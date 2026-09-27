"""UI-only acceptance with explicit API fixtures; never manages live containers."""
import json
from datetime import UTC, datetime
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from check_responsive import OUTPUT, layouts
from browser_environment import BASE_URL, browser_args


def main() -> None:
    result = {"passed": False, "mode": "UI fixtures; no real container operations", "layouts": [], "errors": []}
    state = {"maintenance": {"enabled": False, "reason": ""}, "service_mode": "ready", "policy_error": False,
             "policy": {"session_minutes": 480, "login_attempts": 5, "lockout_minutes": 15, "min_password_length": 8, "require_admin_totp": False}, "jobs": {}}
    service = {"id": "fixture-node", "service": "worker", "state": "running", "health": "healthy", "cpu_percent": 1.25,
               "memory_bytes": 1024**3, "memory_limit_bytes": 2048 * 1024**2, "cpu_limit": 2, "minimum_memory_mb": 2048,
               "actions": ["start", "stop", "restart"], "image_id": "sha256:fixture"}
    held_requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        page.set_default_timeout(15000)

        def reply(route):
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            method = route.request.method
            status = 200
            if path == "/auth/me":
                value = {"id": "ui-fixture-admin", "login": "fixture", "full_name": "Администратор проверки интерфейса", "short_name": "Проверка", "role": "ADMIN", "service": None, "csrf_token": "fixture", "server_time": datetime.now(UTC).isoformat()}
            elif path == "/admin/policies":
                if method == "PATCH" and state["policy_error"]:
                    status, value = 503, {"error": {"code": "TEST_FAILURE", "message": "Проверка состояния ошибки: повторите сохранение."}}
                else:
                    if method == "PATCH":
                        state["policy"] = route.request.post_data_json
                    value = {"access": state["policy"], "audit_mutable": False, "audit_retention": "indefinite"}
            elif path == "/admin/maintenance":
                if method == "POST":
                    state["maintenance"] = route.request.post_data_json
                value = state["maintenance"]
            elif path == "/admin/operations/services":
                if state["service_mode"] == "loading":
                    held_requests.append(route)
                    return
                if state["service_mode"] == "error":
                    status, value = 503, {"error": {"code": "CONTROL_UNAVAILABLE", "message": "Контроллер недоступен — проверка интерфейса."}}
                else:
                    value = {"items": [service] if state["service_mode"] == "ready" else [], "host": {"cpus": 6, "memory_bytes": 32 * 1024**3, "engine_version": "fixture"}}
            elif path.endswith("/logs"):
                value = {"items": [{"instance": "fixture-node", "text": "Техническое сообщение без персональных данных.\n" + "длинноесообщение" * 50}]}
            elif path == "/admin/operations/jobs":
                body = route.request.post_data_json
                state["jobs"][body["id"]] = {"id": body["id"], "status": "SUCCEEDED"}
                value, status = state["jobs"][body["id"]], 202
            elif path.startswith("/admin/operations/jobs/"):
                value = state["jobs"].get(path.rsplit("/", 1)[1], {"status": "QUEUED"})
            else:
                value = {"items": [], "next_cursor": None}
            route.fulfill(status=status, json=value)

        page.route("**/api/v1/**", reply)
        try:
            page.goto(BASE_URL + "/admin")
            page.get_by_role("button", name="Политики доступа", exact=True).click()
            length = page.get_by_label("Минимальная длина нового пароля", exact=True)
            expect(length).to_have_value("8")
            layouts(page, "technical-policy-fixture", result["layouts"])
            length.fill("12")
            state["policy_error"] = True
            page.get_by_role("button", name="Сохранить политику", exact=True).click()
            expect(page.get_by_text("Проверка состояния ошибки: повторите сохранение.", exact=True)).to_be_visible()
            expect(length).to_have_value("12")
            state["policy_error"] = False
            page.get_by_role("button", name="Сохранить политику", exact=True).click()
            expect(page.get_by_text("Политика применяется ко всем новым запросам.", exact=True)).to_be_visible()
            state["service_mode"] = "loading"
            page.get_by_role("button", name="Управление сервисами", exact=True).click()
            page.wait_for_timeout(100)
            expect(page.get_by_text("Загрузка…", exact=True).last).to_be_visible()
            assert held_requests, "The service request should be pending"
            state["service_mode"] = "ready"
            for request in held_requests:
                reply(request)
            restart = page.get_by_role("button", name="Перезапустить", exact=True)
            expect(restart).to_be_disabled()
            page.get_by_role("button", name="Журнал сервиса", exact=True).click()
            layouts(page, "technical-services-fixture", result["layouts"])
            for value in page.locator("dd").all():
                bounds = value.bounding_box()
                assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 360
            page.get_by_label("Причина обслуживания", exact=True).fill("Проверка интерфейса")
            page.get_by_role("button", name="Включить обслуживание", exact=True).click()
            expect(restart).to_be_enabled()
            restart.click()
            expect(page.get_by_text("Техническая операция: Завершена", exact=True)).to_be_visible()
            state["service_mode"] = "error"
            expect(page.get_by_text("Контроллер недоступен — проверка интерфейса.", exact=True)).to_be_visible(timeout=12000)
            layouts(page, "technical-error-fixture", result["layouts"])
            state["service_mode"] = "empty"
            expect(page.get_by_text("Сервисы проекта не найдены.", exact=True)).to_be_visible(timeout=12000)
            layouts(page, "technical-empty-fixture", result["layouts"])
            assert not result["errors"], result["errors"]
            result["passed"] = True
            print("PASS: UI fixture checks, no live service operations", flush=True)
        finally:
            (OUTPUT / "technical-ui-fixtures.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()


if __name__ == "__main__":
    main()
