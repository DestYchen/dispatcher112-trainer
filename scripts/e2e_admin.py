"""Browser acceptance for administration, report/PDF and real health degradation."""
import json
import subprocess
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1600, "height": 1100})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://localhost:5173")
    page.get_by_label("Логин", exact=True).fill("admin")
    page.get_by_label("Пароль", exact=True).fill("admin")
    page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
    expect(page.get_by_role("heading", name="Администрирование", exact=True)).to_be_visible()
    login = "browser_admin_" + str(int(time.time()))
    page.get_by_label("Логин", exact=True).fill(login)
    page.get_by_label("Фамилия", exact=True).fill("Проверяев")
    page.get_by_label("Имя", exact=True).fill("Иван")
    page.get_by_label("Служба", exact=True).select_option(label="ДДС Чертаново Южное")
    page.get_by_label("Новый пароль, не менее 8 символов", exact=True).fill("browser-password")
    page.get_by_role("button", name="Сохранить пользователя", exact=True).click()
    row = page.get_by_role("row").filter(has_text=login)
    expect(row).to_be_visible()
    row.get_by_role("button", name="Изменить", exact=True).click()
    page.get_by_label("Имя", exact=True).fill("Пётр")
    page.get_by_role("button", name="Сохранить пользователя", exact=True).click()
    expect(row).to_contain_text("Пётр")
    row.get_by_role("button", name="Заблокировать", exact=True).click()
    expect(row.get_by_role("button", name="Разблокировать", exact=True)).to_be_visible()
    row.get_by_role("button", name="Разблокировать", exact=True).click()
    page.get_by_role("button", name="Рабочие места", exact=True).click()
    station = "БР-" + str(int(time.time()))[-6:]
    page.get_by_label("Номер рабочего места", exact=True).fill(station)
    page.get_by_label("Кабинет", exact=True).fill("Браузерная проверка")
    page.get_by_role("button", name="Добавить рабочее место", exact=True).click()
    expect(page.get_by_role("cell", name=station, exact=True)).to_be_visible()
    for title, filename in (("Справочник улиц", "streets.csv"), ("Классификатор", "classifier.xlsx")):
        page.get_by_role("button", name=title, exact=True).click()
        page.get_by_label("Файл справочника", exact=True).set_input_files("data/" + filename)
        page.get_by_role("button", name="Импортировать", exact=True).click()
        expect(page.get_by_text("Импорт завершён.", exact=True)).to_be_visible(timeout=20000)
    page.get_by_role("button", name="Журнал аудита", exact=True).click()
    expect(page.get_by_role("cell", name="CLASSIFIER_IMPORTED", exact=True).first).to_be_visible()
    page.get_by_role("button", name="Состояние системы", exact=True).click()
    language_row = page.get_by_role("row").filter(has_text="Проверка грамотности")
    expect(language_row).to_contain_text("Работает", timeout=20000)
    try:
        subprocess.run(["docker", "compose", "stop", "languagetool"], check=True, capture_output=True)
        start = time.perf_counter()
        expect(language_row).to_contain_text("Недоступен", timeout=9000)
        health_latency = time.perf_counter() - start
        assert health_latency < 10
        Path("artifacts/stage10-health.json").write_text(json.dumps({"failure_seconds": health_latency}), encoding="utf-8")
        page.screenshot(path="artifacts/stage10-health.png", full_page=True)
    finally:
        subprocess.run(["docker", "compose", "up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "languagetool"], check=True, capture_output=True)
    page.get_by_role("button", name="Выйти", exact=True).click()
    page.get_by_label("Логин", exact=True).fill("teacher")
    page.get_by_label("Пароль", exact=True).fill("teacher")
    page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
    page.get_by_role("row").filter(has_text="Проверка телефонии").first.get_by_role("button", name="Отчёт о практическом занятии", exact=True).click()
    table = page.get_by_role("table", name="Отчёт о практическом занятии", exact=True)
    expect(table).to_be_visible()
    expect(table.get_by_role("columnheader")).to_have_count(7)
    headers = table.get_by_role("columnheader").all_text_contents()
    page.screenshot(path="artifacts/stage10-report-before.png", full_page=True)
    assert headers == ["Фамилия", "АРМ", "Время", "Ошибки", "Орфогр.", "Уровень", "Балл"], headers
    with page.expect_download() as download:
        page.get_by_role("button", name="Скачать PDF", exact=True).click()
    download.value.save_as("artifacts/stage10-browser.pdf")
    assert Path("artifacts/stage10-browser.pdf").read_bytes().startswith(b"%PDF")
    table.get_by_role("button").first.click()
    expect(page.get_by_role("heading", name="Корректировка оценки", exact=True)).to_be_visible()
    page.screenshot(path="artifacts/stage10-report.png", full_page=True)
    assert not errors, errors
    browser.close()
Path("artifacts/stage10-browser.json").write_text(json.dumps({"health_failure_seconds": round(health_latency, 3), "seven_columns": True, "pdf_download": True, "page_errors": errors}), encoding="utf-8")
print("PASS: administration, imports, audit, real health, report, PDF", round(health_latency, 3))
