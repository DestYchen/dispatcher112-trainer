"""Four independent Chromium contexts exercise the prepared three-student demo."""
import json
import time
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    contexts = [browser.new_context(viewport={"width": 1440, "height": 1000}) for _ in range(4)]
    pages = [context.new_page() for context in contexts]
    errors = []
    for index, page in enumerate(pages):
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://localhost:5173")
        page.get_by_label("Логин", exact=True).fill("teacher" if index == 0 else f"student{index}")
        page.get_by_label("Пароль", exact=True).fill("teacher" if index == 0 else "student")
        page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
        expect(page.get_by_role("button", name="Выйти")).to_be_visible()
    teacher = pages[0]
    row = teacher.get_by_role("row").filter(has_text="Демонстрация АРМ-112: три рабочих места").filter(has=teacher.get_by_role("button", name="Начать занятие"))
    expect(row).to_have_count(1)
    row.get_by_role("button", name="Начать занятие").click()
    row = teacher.get_by_role("row").filter(has_text="Демонстрация АРМ-112: три рабочих места").filter(has=teacher.get_by_role("button", name="Завершить занятие"))
    row.get_by_role("button", name="Открыть пульт", exact=True).click()
    table = teacher.get_by_role("table", name="Участники занятия", exact=True)
    expect(table.get_by_role("row")).to_have_count(4)
    states = []
    latencies = []
    for index, page in enumerate(pages[1:], 7):
        first = page.get_by_role("navigation", name="Активные").get_by_role("button").first
        expect(first).to_be_visible(timeout=4000)
        first.click()
        expect(page.get_by_role("button", name="Принята", exact=True)).to_be_visible()
        page.keyboard.press("Alt+1")
        started = time.monotonic()
        page.get_by_role("button", name="Сохранить статус", exact=True).click()
        expect(page.get_by_role("button", name="Реагирование начато", exact=True)).to_be_visible()
        expect(table.get_by_role("row").filter(has_text=f"АРМ-{index:02d}").get_by_role("cell").last).to_contain_text("Принята", timeout=2000)
        latencies.append(round(time.monotonic() - started, 3))
        assert latencies[-1] < 2
        state = page.evaluate("async () => (await fetch('/api/v1/student/state')).json()")
        assert state["workstation"]["number"] == f"АРМ-{index:02d}"
        states.append(state)
    assert len({state["cards"][0]["assignment_id"] for state in states}) == 3
    assert len({state["lesson"]["id"] for state in states}) == 1
    teacher.screenshot(path="artifacts/stage11-demo-teacher.png", full_page=True)
    pages[1].screenshot(path="artifacts/stage11-demo-student.png", full_page=True)
    row.get_by_role("button", name="Завершить занятие", exact=True).click()
    for page in pages[1:]:
        expect(page.get_by_text("Занятие начнёт преподаватель.", exact=True)).to_be_visible(timeout=15000)
    assert not errors, errors
    browser.close()
    Path("artifacts/stage11-demo.json").write_text(json.dumps({"students": 3, "distinct_assignments": 3,
        "stations": [state["workstation"]["number"] for state in states], "action_latency_seconds": latencies, "page_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: demo with three independent students, assigned workstations, statuses, live console and finish")
