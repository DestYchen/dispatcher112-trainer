"""One incoming call with retained PBX contact diagnostics and server timing checks."""
import json
import subprocess
import time
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
from check_responsive import api, login, OUTPUT


def main():
    fixture = json.loads(Path("data/sip-acceptance.json").read_text(encoding="utf-8"))
    person = fixture["participants"][2]
    result = {"passed": False, "sip_frames": []}
    running = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        teacher = browser.new_context().new_page()
        student = browser.new_context(permissions=["microphone"]).new_page()
        def socket_open(socket):
            if not socket.url.endswith('/sip'):
                return
            def frame(payload):
                lines = str(payload).split('\r\n')
                result['sip_frames'].append([line for index, line in enumerate(lines) if index == 0 or line.lower().startswith(('contact:', 'cseq:', 'via:'))])
            socket.on('framereceived', frame)
            socket.on('framesent', frame)
        student.on('websocket', socket_open)
        try:
            login(teacher, "teacher", "teacher")
            login(student, person["login"], "student")
            student.get_by_role("button", name="Включить гарнитуру", exact=True).click()
            expect(student.get_by_text("Телефон готов", exact=True)).to_be_visible(timeout=15000)
            result["contacts"] = subprocess.check_output(["docker", "compose", "exec", "-T", "telephony", "asterisk", "-rx", "pjsip show contacts"], encoding="utf-8")
            print(result["contacts"], flush=True)
            scenarios = api(teacher, "/teacher/scenarios?status=APPROVED&limit=200")["items"]
            scenario = next(row for row in scenarios if row["title"] == "Учебный пожар: двор")
            lesson = api(teacher, "/teacher/lessons", "POST", {"title": "SIP incoming " + str(int(time.time())), "participants": [{"student_id": person["id"]}], "settings": {"training_mode": "CARD_ENTRY", "incoming_channel": "VOICE", "primary_status_deadline_sec": 3600, "card_processing_deadline_sec": 3600, "grammar_check_enabled": False}})
            lesson_id = lesson["id"]
            assignment_id = api(teacher, f"/teacher/lessons/{lesson_id}/assign", "POST", {"assignments": [{"student_id": person["id"], "scenario_id": scenario["id"]}]})["assignment_ids"][0]
            api(teacher, f"/teacher/lessons/{lesson_id}/start", "POST")
            running = True
            student.get_by_role("navigation", name="Активные", exact=True).get_by_role("button").first.click()
            student.wait_for_timeout(5000)
            result["calls_before"] = api(student, f"/sip/assignments/{assignment_id}/calls")
            print(result["calls_before"], flush=True)
            result["contacts_after"] = subprocess.check_output(["docker", "compose", "exec", "-T", "telephony", "asterisk", "-rx", "pjsip show contacts"], encoding="utf-8")
            print(result["contacts_after"], flush=True)
            result['contact_details'] = subprocess.check_output(['docker','compose','exec','-T','telephony','asterisk','-rx','database show registrar/contact'], encoding='utf-8')
            expect(student.get_by_role("button", name="Ответить на вызов", exact=True)).to_be_visible(timeout=10000)
            student.get_by_role("button", name="Ответить на вызов", exact=True).click()
            expect(student.get_by_label("Имя заявителя", exact=True)).to_be_enabled(timeout=15000)
            student.wait_for_timeout(6000)
            student.get_by_role("button", name="Завершить вызов", exact=True).click()
            student.wait_for_timeout(3000)
            result["calls_after"] = api(student, f"/sip/assignments/{assignment_id}/calls")
            assert result["calls_after"]["calls"][0]["recording_available"]
            result["passed"] = True
        finally:
            student.screenshot(path=str(OUTPUT / "sip-incoming.png"))
            (OUTPUT / "sip-incoming.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if running:
                api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            browser.close()


if __name__ == "__main__":
    main()
