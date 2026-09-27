"""Actual outgoing SIP call, report, authorization and the complete DDS status cycle."""
import json
import time
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
from check_responsive import api, login, layouts, OUTPUT
from browser_environment import BASE_URL, browser_args


def main():
    fixture = json.loads(Path("data/sip-acceptance.json").read_text(encoding="utf-8"))
    person = fixture["participants"][0]
    result = {"passed": False, "layouts": [], "errors": []}
    running = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=[*browser_args(), "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        teacher = browser.new_context().new_page()
        student = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1100}).new_page()
        student.on("pageerror", lambda error: result["errors"].append(str(error)))
        try:
            login(teacher, fixture["teacher"], "teacher")
            login(student, person["login"], "student")
            student.get_by_role("button", name="Включить гарнитуру", exact=True).click()
            expect(student.get_by_text("Телефон готов", exact=True)).to_be_visible(timeout=15000)
            scenario = next(row for row in api(teacher, "/teacher/scenarios?status=APPROVED&limit=200")["items"] if row["title"] == "Учебный пожар: доклад дежурному")
            lesson_id = api(teacher, "/teacher/lessons", "POST", {"title": "SIP outgoing " + str(int(time.time())), "participants": [{"student_id": person["id"]}], "settings": {"primary_status_deadline_sec": 3600, "card_processing_deadline_sec": 3600, "grammar_check_enabled": False}})["id"]
            assignment_id = api(teacher, f"/teacher/lessons/{lesson_id}/assign", "POST", {"assignments": [{"student_id": person["id"], "scenario_id": scenario["id"]}]})["assignment_ids"][0]
            api(teacher, f"/teacher/lessons/{lesson_id}/start", "POST")
            running = True
            student.get_by_role("navigation", name="Активные", exact=True).get_by_role("button").first.click()
            expect(student.get_by_role("button", name="Телефония · Alt+T", exact=True)).to_be_enabled()
            student.keyboard.press("Alt+T")
            student.get_by_label("Номер абонента", exact=True).fill("2201")
            student.get_by_role("button", name="Вызов", exact=True).click()
            expect(student.get_by_text("Идёт разговор · ведётся учебная запись", exact=True)).to_be_visible(timeout=15000)
            report = student.get_by_role("textbox", name="Ваш доклад", exact=True)
            report.fill("Пожар, ул. Дубнинская, д. 28. Пострадавших нет. Бригада направлена.")
            layouts(student, "sip-outgoing", result["layouts"])
            student.get_by_role("button", name="Завершить доклад", exact=True).click()
            expect(student.get_by_text("Я вас понял, информация принята.", exact=True)).to_be_visible(timeout=15000)
            student.keyboard.press("Escape")
            for status in ("Принята", "Реагирование начато", "Прибыли", "Работы ведутся", "Работы завершены 🔒"):
                student.get_by_role("button", name=status, exact=True).click()
                student.get_by_role("button", name="Сохранить статус", exact=True).click()
                if status.endswith("🔒"):
                    student.get_by_role("dialog").get_by_role("button", name="Закрыть карточку", exact=True).click()
                else:
                    expect(student.get_by_role("button", name=status, exact=True)).not_to_be_visible()
            expect(student.get_by_text("Карточка закрыта", exact=True).first).to_be_visible()
            detail = api(teacher, f"/teacher/assignments/{assignment_id}")
            assert detail["score"]["axes"]["completeness"]["score"] == 100
            reports = [event for event in detail["events"] if event["kind"] == "PHONE_REPORT"]
            assert len(reports) == 1 and reports[0]["payload"]["duration_ms"] > 0
            call = detail["sip_calls"][0]
            assert call["state"] == "ENDED" and call["recording_available"]
            recording = teacher.request.get(f"{BASE_URL}/api/v1/sip/calls/{call['id']}/recording")
            assert recording.status == 200 and recording.body()[:4] == b"RIFF"
            result.update(passed=True, call_id=call["id"], reports=1, completeness=100, server_duration_ms=reports[0]["payload"]["duration_ms"])
            assert not result["errors"]
            print("PASS: outgoing SIP, recorded report, five status transitions, seven responsive layouts", flush=True)
        finally:
            student.screenshot(path=str(OUTPUT / "sip-outgoing-final.png"))
            if running:
                api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            (OUTPUT / "sip-outgoing.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()


if __name__ == "__main__":
    main()
