"""Explicit diagnostic UI fixtures: loading/error/empty/data, export and pagination."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, browser_args
from check_responsive import OUTPUT, layouts


def main():
    result = {
        "passed": False,
        "mode": "explicit diagnostic API fixtures",
        "layouts": [],
        "errors": [],
    }
    state = {"mode": "loading", "export_error": False}
    held = []
    report = {
        "generated_at": "2026-09-26T10:00:00Z",
        "period": {"from": "2026-09-25T10:00:00Z", "to": "2026-09-26T10:00:00Z"},
        "usage": {
            "active_users": 5,
            "lessons_started": 1,
            "assignments_delivered": 10,
            "assignments_closed": 8,
            "sip_calls": 4,
            "generation_jobs": 2,
            "audit_events": 100,
        },
        "inventory": {
            "users_total": 8,
            "users_active": 7,
            "workstations_active": 3,
            "lessons_running": 0,
        },
        "http": {
            "since": "2026-09-26T09:00:00Z",
            "requests": 120,
            "server_errors": 2,
            "mean_ms": 20.5,
            "over_two_seconds": 0,
            "journal_write_failures": 0,
        },
        "integrity": [
            {"code": key, "status": "OK"}
            for key in (
                "schema",
                "audit",
                "application_role",
                "backup_role",
                "foreign_keys",
                "backup_catalog",
            )
        ],
        "failures": {"total": 2, "next_cursor": "MQ==", "items": []},
    }
    first = {
        "at": "2026-09-26T09:50:00Z",
        "source": "HTTP",
        "reference": "100",
        "request_id": "a" * 32,
        "method": "GET",
        "route": "/admin/operations/services",
        "status": 503,
        "code": "CONTROL_UNAVAILABLE",
    }
    second = {
        "at": "2026-09-26T09:40:00Z",
        "source": "GENERATION",
        "reference": "b" * 32,
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))

        def reply(route):
            parsed = urlparse(route.request.url)
            query = parse_qs(parsed.query)
            if parsed.path.endswith("/auth/me"):
                value = {
                    "id": "diagnostic-fixture-admin",
                    "login": "fixture",
                    "full_name": "Администратор проверки",
                    "short_name": "Проверка",
                    "role": "ADMIN",
                    "service": None,
                    "csrf_token": "fixture",
                    "server_time": datetime.now(UTC).isoformat(),
                }
            elif parsed.path.endswith("/admin/diagnostics/report"):
                if state["mode"] == "loading":
                    held.append(route)
                    return
                if state["mode"] == "error" or (
                    "export" in query and state["export_error"]
                ):
                    route.fulfill(
                        status=503,
                        json={
                            "error": {
                                "code": "INTERNAL_ERROR",
                                "message": "Диагностика временно недоступна.",
                            }
                        },
                    )
                    return
                value = copy.deepcopy(report)
                value["failures"]["items"] = [second] if "cursor" in query else [first]
                if "cursor" in query:
                    value["failures"]["next_cursor"] = None
                if "export" in query:
                    value["failures"] = {
                        "total": 2,
                        "items": [first, second],
                        "next_cursor": None,
                    }
                if state["mode"] == "empty":
                    value["usage"] = {key: 0 for key in value["usage"]}
                    value["integrity"][-1]["status"] = "EMPTY"
                    value["failures"] = {"total": 0, "items": [], "next_cursor": None}
                if state["mode"] == "degraded":
                    value["integrity"][1]["status"] = "FAIL"
                    value["integrity"][-1]["status"] = "UNAVAILABLE"
                    value["http"]["journal_write_failures"] = 1
            else:
                value = {"items": [], "next_cursor": None}
            route.fulfill(json=value)

        page.route("**/api/v1/**", reply)
        try:
            page.goto(BASE_URL + "/admin")
            page.get_by_role("button", name="Технический отчёт", exact=True).click()
            expect(page.get_by_text("Загрузка…", exact=True)).to_be_visible()
            layouts(page, "diagnostics-loading", result["layouts"])
            state["mode"] = "ready"
            for route in held:
                reply(route)
            expect(page.get_by_text("a" * 32, exact=True)).to_be_visible()
            layouts(page, "diagnostics-ready", result["layouts"])
            page.get_by_role("button", name="Следующая страница", exact=True).click()
            expect(page.get_by_text("b" * 32, exact=True)).to_be_visible()
            expect(page.get_by_text("a" * 32, exact=True)).to_have_count(0)
            state["export_error"] = True
            page.get_by_role(
                "button", name="Скачать полный отчёт JSON", exact=True
            ).click()
            expect(
                page.get_by_text("Диагностика временно недоступна.", exact=True)
            ).to_be_visible()
            state["export_error"] = False
            with page.expect_download() as pending:
                page.get_by_role(
                    "button", name="Скачать полный отчёт JSON", exact=True
                ).click()
            downloaded = json.loads(
                Path(pending.value.path()).read_text(encoding="utf-8")
            )
            assert len(downloaded["failures"]["items"]) == 2
            result["full_export_from_second_page"] = True
            state["mode"] = "empty"
            page.get_by_role("button", name="Сформировать отчёт", exact=True).click()
            expect(
                page.get_by_text(
                    "За выбранный период технические сбои не зарегистрированы.",
                    exact=True,
                )
            ).to_be_visible()
            layouts(page, "diagnostics-empty", result["layouts"])
            expect(
                page.get_by_text("Нет копий для проверки", exact=True)
            ).to_be_visible()
            fields = page.locator('input[type="datetime-local"]')
            fields.nth(0).fill("2026-09-20T10:00")
            fields.nth(1).fill("2026-09-26T10:00")
            state["mode"] = "error"
            page.get_by_role("button", name="Сформировать отчёт", exact=True).click()
            expect(
                page.get_by_text("Диагностика временно недоступна.", exact=True)
            ).to_be_visible()
            expect(fields.nth(0)).to_have_value("2026-09-20T10:00")
            layouts(page, "diagnostics-error", result["layouts"])
            state["mode"] = "degraded"
            page.get_by_role("button", name="Проверить снова", exact=True).click()
            expect(page.get_by_text("Обнаружено нарушение", exact=True)).to_be_visible()
            expect(page.get_by_text("Не удалось проверить", exact=True)).to_be_visible()
            expect(fields.nth(0)).to_have_value("2026-09-20T10:00")
            layouts(page, "diagnostics-degraded", result["layouts"])
            assert not result["errors"], result["errors"]
            result["passed"] = True
        finally:
            (OUTPUT / "diagnostics-ui.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            browser.close()


if __name__ == "__main__":
    main()
