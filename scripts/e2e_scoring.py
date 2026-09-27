"""Browser acceptance for contextual address checks and scored lesson results."""
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

api = httpx.Client(base_url="http://localhost:8000/api/v1")
pre = api.get("/auth/me").json()["error"]["details"]["csrf_token"]
login = api.post("/auth/login", headers={"X-CSRF-Token": pre}, json={"login": "teacher", "password": "teacher"})
login.raise_for_status()
api.headers["X-CSRF-Token"] = login.json()["csrf_token"]
student_id = api.get("/teacher/participants").json()["students"][0]["id"]
scenario = next(row for row in api.get("/teacher/scenarios?status=APPROVED").json()["items"] if row["title"] == "Учебный пожар: уточнение адреса")
title = "Проверка адреса " + str(int(time.time()))
created = api.post("/teacher/lessons", json={"title": title, "participants": [{"student_id": student_id}]})
created.raise_for_status()
lesson_id = created.json()["id"]
api.post(f"/teacher/lessons/{lesson_id}/assign", json={"assignments": [{"student_id": student_id, "scenario_id": scenario["id"]}]}).raise_for_status()
api.post(f"/teacher/lessons/{lesson_id}/start").raise_for_status()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://localhost:5173")
    page.get_by_label("Логин", exact=True).fill("student1")
    page.get_by_label("Пароль", exact=True).fill("student")
    page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
    queue = page.get_by_role("navigation", name="Активные")
    queue.get_by_role("button").first.click()
    page.get_by_role("textbox").fill("Работы на Дубининской улице выполнены.")
    issue = page.get_by_role("button", name="Дубининской: Улица отличается от адреса карточки. Проверьте название.", exact=True)
    expect(issue).to_be_visible(timeout=5000)
    issue.click()
    expect(page.get_by_text("Дубнинская улица", exact=True)).to_be_visible()
    page.screenshot(path="artifacts/stage6-address.png", full_page=True)
    for status in ("Принята", "Реагирование начато", "Прибыли", "Работы ведутся", "Работы завершены 🔒"):
        page.get_by_role("button", name=status, exact=True).click()
        page.get_by_role("button", name="Сохранить статус", exact=True).click()
        if status.endswith("🔒"):
            page.get_by_role("dialog").get_by_role("button", name="Закрыть карточку", exact=True).click()
        else:
            expect(page.get_by_role("button", name=status, exact=True)).not_to_be_visible()
    expect(page.get_by_text("Карточка закрыта", exact=True).first).to_be_visible()
    api.post(f"/teacher/lessons/{lesson_id}/finish").raise_for_status()
    expect(page.get_by_role("heading", name="Результаты занятия", exact=True)).to_be_visible()
    expect(page.get_by_text("Сверьте название улицы с адресом карточки и местным справочником.", exact=True)).to_be_visible()
    page.screenshot(path="artifacts/stage6-results.png", full_page=True)
    result = page.evaluate("async id => await (await fetch('/api/v1/student/results?lesson_id='+id)).json()", lesson_id)
    assert result["cards"][0]["score"]["axes"]["literacy"]["score"] < 100
    assert any(item["code"] == "ADDRESS_TYPO" for item in result["cards"][0]["violations"])
    assert not errors, errors
    browser.close()
api.close()
Path("artifacts/stage6-browser.txt").write_text("PASS: contextual address check, full status chain, persisted score and result hints", encoding="utf-8")
print("PASS: contextual address check, full status chain, persisted score and result hints")
