"""Real Chromium acceptance: keyboard flow, API guards and 20s offline recovery.

Run against seeded local Compose: python scripts/e2e_student.py
Requires Playwright with Chromium installed on the test machine.
"""
import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

artifacts = Path(__file__).resolve().parents[1] / "artifacts"
artifacts.mkdir(exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    contexts = [browser.new_context(viewport={"width": 1280, "height": 1000}) for _ in range(2)]
    teacher, student = [context.new_page() for context in contexts]
    for page, login, password in [(teacher, "teacher", "teacher"), (student, "student1", "student")]:
        page.goto("http://localhost:5173")
        page.get_by_label("Логин", exact=True).fill(login)
        page.get_by_label("Пароль", exact=True).fill(password)
        page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
        expect(page.get_by_role("button", name="Выйти")).to_be_visible()
    title = "Приёмка карточки " + str(int(time.time()))
    teacher.get_by_text("Новое занятие", exact=True).click()
    teacher.get_by_label("Название", exact=True).fill(title)
    teacher.get_by_label("Обучающийся", exact=True).select_option(label="Иванов Иван")
    teacher.get_by_role("button", name="Создать занятие", exact=True).click()
    teacher.get_by_role("button", name="Назначить подготовленные сценарии", exact=True).click()
    expect(teacher.get_by_role("button", name="Создать занятие", exact=True)).to_be_visible()
    row = teacher.get_by_role("row").filter(has_text=title)
    row.get_by_role("button", name="Начать занятие").click()
    queue = student.get_by_role("navigation", name="Активные")
    first = queue.get_by_role("button").first
    expect(first).to_be_visible(timeout=2000)
    first.focus()
    student.keyboard.press("Enter")
    expect(student.get_by_role("button", name="Принята", exact=True)).to_be_visible()
    student.keyboard.press("Alt+2")
    expect(student.get_by_role("textbox")).to_be_focused()
    student.get_by_role("textbox").fill("Короткий отказ")
    expect(student.get_by_role("button", name="Сохранить статус")).to_be_disabled()
    checks = student.evaluate("""async () => {
      const me = await (await fetch('/api/v1/auth/me')).json();
      const state = await (await fetch('/api/v1/student/state')).json();
      const id = state.cards[0].assignment_id;
      const send = async (status, comment) => {
        const response = await fetch(`/api/v1/student/assignments/${id}/status`, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf_token,'Idempotency-Key':crypto.randomUUID()}, body:JSON.stringify({status,comment})});
        return response.status;
      };
      return {id, invalid:await send('ARRIVED',null), refusal:await send('NOT_ACCEPTED','Коротко')};
    }""")
    assert checks["invalid"] == 409 and checks["refusal"] == 422, checks
    student.keyboard.press("Alt+1")
    student.get_by_role("button", name="Сохранить статус").focus()
    student.keyboard.press("Enter")
    expect(student.get_by_role("button", name="Реагирование начато", exact=True)).to_be_visible()
    timer_before = student.get_by_test_id("processing-timer").inner_text()
    contexts[1].set_offline(True)
    student.get_by_role("button", name="Реагирование начато", exact=True).focus()
    student.keyboard.press("Enter")
    student.get_by_role("button", name="Сохранить статус").focus()
    student.keyboard.press("Enter")
    student.wait_for_timeout(20000)
    assert student.get_by_test_id("processing-timer").inner_text() != timer_before
    queued_before = student.evaluate("Object.entries(localStorage).filter(([key])=>key.startsWith('dispatcher112-actions:')).map(([key,value])=>JSON.parse(value)).flat()")
    assert len(queued_before) == 1
    contexts[1].set_offline(False)
    retry = student.get_by_role("button", name="Повторить сейчас").first
    if retry.is_visible():
        retry.click()
    expect(student.get_by_role("button", name="Прибыли", exact=True)).to_be_enabled(timeout=20000)
    student.wait_for_function("Object.entries(localStorage).filter(([key])=>key.startsWith('dispatcher112-actions:')).every(([key,value])=>JSON.parse(value).length===0)")
    student.get_by_role("button", name="Работы завершены 🔒", exact=True).focus()
    student.keyboard.press("Enter")
    student.get_by_role("button", name="Сохранить статус").focus()
    student.keyboard.press("Enter")
    expect(student.get_by_role("dialog")).to_be_visible()
    expect(student.get_by_role("button", name="Вернуться", exact=True)).to_be_focused()
    student.keyboard.press("Escape")
    expect(student.get_by_role("dialog")).not_to_be_visible()
    student.get_by_role("button", name="Сохранить статус").focus()
    student.keyboard.press("Enter")
    student.keyboard.press("Tab")
    student.keyboard.press("Enter")
    expect(student.get_by_text("Карточка закрыта", exact=True).first).to_be_visible()
    payload = student.evaluate("async id => await (await fetch('/api/v1/student/assignments/'+id)).json()", checks["id"])
    statuses = [event["status"] for event in payload["my_block"]["history"] if not event["is_automatic"]]
    assert statuses == ["ACCEPTED", "RESPONSE_STARTED", "WORK_COMPLETED"], statuses
    student.screenshot(path=str(artifacts / "stage5-card.png"), full_page=True)
    row.get_by_role("button", name="Завершить занятие").click()
    expect(student.get_by_text("Занятие начнёт преподаватель.", exact=True)).to_be_visible()
    browser.close()
    (artifacts / "stage5-e2e.json").write_text(json.dumps({"api_guards": checks, "offline_seconds": 20, "statuses": statuses}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: keyboard cycle, required comment, 409/422 API guards, modal focus/Escape, server timers and 20s offline replay without duplicates")
