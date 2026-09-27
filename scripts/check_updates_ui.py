"""Explicit update UI fixtures; geometry and interactions do not claim a real installation."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, browser_args
from check_responsive import OUTPUT, layouts


def main():
    result = {"passed": False, "mode": "explicit software update API fixtures", "layouts": [], "errors": []}
    now = datetime.now(UTC)
    package = {"id": str(uuid4()), "version": "1.2.3", "description": "Исправление обработки учебных карточек и проверок обновления программы", "size": 8123456, "filename": "release.zip", "uploaded_at": now.isoformat(), "program_files": 430, "sha256": "a" * 64, "publisher_sha256": "b" * 64}
    value = {"trust": {"configured": True, "fingerprint": "b" * 64, "error": None}, "limits": {"archive_bytes": 264 * 1024 * 1024, "packages": 20, "storage_bytes": 2 ** 31}, "packages": [package], "updates": [], "current": None, "maintenance": {"enabled": False, "reason": ""}, "execution": "LOCAL_COMMAND"}
    state = {"mode": "loading", "request_error": True}
    held = []
    requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))

        def reply(route):
            path = urlparse(route.request.url).path
            if path.endswith("/auth/me"):
                route.fulfill(json={"id": str(uuid4()), "login": "fixture", "full_name": "Администратор проверки", "short_name": "Проверка", "role": "ADMIN", "service": None, "csrf_token": "fixture", "server_time": now.isoformat()})
            elif path.endswith("/updates/requests"):
                body = route.request.post_data_json
                requests.append(body)
                if state["request_error"]:
                    route.fulfill(status=503, json={"error": {"message": "Проверочный сбой запроса"}})
                else:
                    signed = {**body, "schema": "software-update-request-1", "version": package["version"], "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=24)).isoformat()}
                    route.fulfill(json={"request": {"value": signed, "signature": "explicit-ui-fixture"}, "filename": f"software-update-{body['id']}.json", "execution": "LOCAL_COMMAND"})
            elif path.endswith("/operations/updates"):
                if state["mode"] == "loading":
                    held.append(route)
                    return
                if state["mode"] == "error":
                    route.fulfill(status=503, json={"error": {"message": "Проверочный сервер недоступен"}})
                    return
                response = copy.deepcopy(value)
                if state["mode"] in {"empty", "untrusted"}:
                    response["packages"] = []
                if state["mode"] == "untrusted":
                    response["trust"].update(configured=False, fingerprint=None)
                if state["mode"] == "ready":
                    response["current"] = {"id": str(uuid4()), "version": "1.2.3", "phase": "READY"}
                    response["maintenance"] = {"enabled": True, "reason": "Проверка новой версии программы", "update_id": response["current"]["id"]}
                    response["updates"] = [{"id": response["current"]["id"], "version": "1.2.3", "phase": "READY", "reason": "Плановое обновление учебной программы", "at": now.isoformat()}]
                route.fulfill(json=response)
            else:
                route.fulfill(json={"items": [], "next_cursor": None})

        page.route("**/api/v1/**", reply)
        try:
            page.goto(BASE_URL + "/admin")
            page.get_by_role("button", name="Обновления программы", exact=True).click()
            expect(page.get_by_text("Загрузка…", exact=True).first).to_be_visible()
            layouts(page, "updates-loading", result["layouts"])
            state["mode"] = "data"
            for route in held:
                reply(route)
            held.clear()
            expect(page.get_by_text(package["description"]).first).to_be_visible()
            layouts(page, "updates-data", result["layouts"])
            page.get_by_label("Пакет для установки", exact=True).select_option(package["id"])
            page.get_by_label("Причина действия", exact=True).fill("Плановое обновление программы после проверки")
            page.get_by_role("button", name="Подготовить подписанный запрос", exact=True).click()
            expect(page.get_by_text("Проверочный сбой запроса", exact=True)).to_be_visible()
            expect(page.get_by_label("Причина действия", exact=True)).to_have_value("Плановое обновление программы после проверки")
            state["request_error"] = False
            page.get_by_role("button", name="Проверить снова", exact=True).click()
            expect(page.get_by_role("heading", name="Запрос подготовлен; выполнение на сервере ещё не запущено.", exact=True)).to_be_visible()
            assert requests[0] == requests[1]
            layouts(page, "updates-request", result["layouts"])
            with page.expect_download() as download:
                page.get_by_role("button", name="Скачать подписанный запрос", exact=True).click()
            exported = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
            assert exported["value"]["id"] == requests[0]["id"]
            result["reason_and_request_identity_preserved"] = True
            result["request_downloaded"] = True
            page.get_by_role("button", name="Создать другой запрос", exact=True).click()
            for mode in ("ready", "empty", "untrusted", "error"):
                state["mode"] = mode
                page.get_by_role("button", name="Обновить состояние", exact=True).click()
                if mode == "ready":
                    expect(page.get_by_text("Проверьте новую версию в режиме обслуживания, затем подготовьте запрос на активацию. До активации можно запросить откат.", exact=True)).to_be_visible()
                elif mode == "empty":
                    expect(page.get_by_text("Пакеты ещё не загружены. Выберите ZIP-файл издателя и проверьте его подпись.", exact=True)).to_be_visible()
                elif mode == "untrusted":
                    expect(page.get_by_text("На сервере не настроен доверенный ключ издателя. Передайте оператору открытый ключ по доверенному каналу. После настройки обновите страницу.", exact=True)).to_be_visible()
                else:
                    expect(page.get_by_text("Проверочный сервер недоступен", exact=True).first).to_be_visible()
                layouts(page, "updates-" + mode, result["layouts"])
            assert not result["errors"]
            result["passed"] = True
        finally:
            (OUTPUT / "updates-ui.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
    print(json.dumps({"passed": result["passed"], "layouts": len(result["layouts"])}))


if __name__ == "__main__":
    main()
