"""Scenario revision and group/criteria UI acceptance, using the real server."""
import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from check_responsive import OUTPUT, api, layouts, login


def main():
    fixture = json.loads(Path("data/learning-acceptance.json").read_text(encoding="utf-8"))
    result = {"passed": False, "layouts": [], "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        page.set_default_timeout(20000)
        try:
            login(page, fixture["teacher"], "teacher")
            page.get_by_role("link", name="Обучение и история", exact=True).click()
            page.locator("summary").filter(has_text="Банк сценариев").click()
            source = page.locator(f'[data-scenario-id="{fixture["scenario_id"]}"]')
            with page.expect_response(f"**/scenarios/{fixture['scenario_id']}/copy") as response:
                source.get_by_role("button", name="Создать исправленную версию", exact=True).click()
            copied_id = response.value.json()["id"]
            editor = page.get_by_role("region", name="Эталонное решение", exact=True)
            name = "Проверенная версия " + str(int(time.time()))
            editor.get_by_label("Название", exact=True).fill(name)
            editor.get_by_label("Описание", exact=True).fill("На улице горит мусор во дворе. Пострадавших нет. Требуется проверка на месте.")
            layouts(page, "learning-scenario-editor", result["layouts"])
            editor.get_by_role("button", name="Подтвердить", exact=True).click()
            expect(editor).not_to_be_visible()
            page.get_by_label("Банк сценариев", exact=True).select_option("APPROVED")
            candidate = page.locator(f'[data-scenario-id="{copied_id}"]')
            expect(candidate.get_by_role("heading", name=name, exact=True)).to_be_visible()
            candidate.get_by_role("button", name="Убрать из новых назначений", exact=True).click()
            expect(candidate).not_to_be_visible()
            page.get_by_label("Показать архивные", exact=True).check()
            expect(candidate).to_be_visible()
            candidate.get_by_role("button", name="Удалить неиспользованный сценарий", exact=True).click()
            candidate.get_by_role("button", name="Подтвердить удаление", exact=True).click()
            expect(candidate).not_to_be_visible()
            page.get_by_role("button", name="Группы", exact=True).click()
            page.get_by_role("button", name="Разбор группы", exact=True).first.click()
            expect(page.get_by_role("heading", name="Результаты группы", exact=True)).to_be_visible()
            layouts(page, "learning-group-analysis", result["layouts"])

            page.get_by_role("link", name="Рабочее место", exact=True).click()
            page.get_by_text("Новое занятие", exact=True).click()
            page.get_by_label("Название", exact=True).fill("Подготовка ИИ " + str(int(time.time())))
            page.locator("select[multiple]").select_option(fixture["participants"][0]["id"])
            page.get_by_role("checkbox", name="Критерии успешности", exact=True).check()
            page.get_by_label("Минимальный балл", exact=True).fill("80")
            page.get_by_label("Минимум слов в ответе", exact=True).fill("3")
            layouts(page, "learning-criteria", result["layouts"])
            with page.expect_response("**/api/v1/teacher/lessons") as response:
                page.get_by_role("button", name="Создать занятие", exact=True).click()
            lesson_id = response.value.json()["id"]
            stored = api(page, f"/teacher/lessons/{lesson_id}")
            assert stored["settings"]["success_criteria"]["min_total"] == 80
            expect(page.get_by_role("heading", name="Подготовка занятия", exact=True)).to_be_visible()
            page.get_by_label("Количество сценариев", exact=True).fill("1")
            page.get_by_role("button", name="Сгенерировать сценарии", exact=True).click()
            expect(page.get_by_role("region", name="Эталонное решение", exact=True)).to_be_visible(timeout=120000)
            prepared = api(page, f"/teacher/scenarios?status=PENDING_REVIEW&lesson_id={lesson_id}")["items"]
            assert prepared and prepared[0]["card_payload"]["generation"]["backend"] == "local_llm"
            result.update(passed=True, lesson_id=lesson_id, generated_scenario_id=prepared[0]["id"], model=prepared[0]["card_payload"]["generation"])
            assert not result["errors"], result["errors"]
            print("PASS: full scenario revision/archive/delete, group analysis, stored criteria, queued local LLM", flush=True)
        finally:
            page.screenshot(path=str(OUTPUT / "learning-revision-final.png"))
            (OUTPUT / "learning-revision.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()


if __name__ == "__main__":
    main()
