"""Exercise the actual entry UI, 30 seconds offline, grading and teacher reuse."""

import json
import time

from playwright.sync_api import expect, sync_playwright

from check_responsive import OUTPUT, api, layouts, login
from browser_environment import context_options, launch_browser


def main():
    cases, errors = [], []
    lesson_id, running, passed = None, False, False
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        teacher_context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, **context_options()
        )
        student_context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, **context_options()
        )
        teacher, student = teacher_context.new_page(), student_context.new_page()
        for page in (teacher, student):
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(teacher, "teacher", "teacher")
            login(student, "student1", "student")
            assert api(student, "/student/state")["lesson"] is None, (
                "Existing lesson must remain untouched"
            )
            me = api(student, "/auth/me")
            teacher.get_by_text("Новое занятие", exact=True).click()
            teacher.get_by_label("Вид упражнения", exact=True).select_option(
                "CARD_ENTRY"
            )
            expect(teacher.get_by_label("Вид упражнения", exact=True)).to_have_value(
                "CARD_ENTRY"
            )
            layouts(teacher, "entry-lesson-form", cases, [1440, 390])
            scenarios = api(teacher, "/teacher/scenarios?status=APPROVED&limit=200")[
                "items"
            ]
            scenario = next(
                row for row in scenarios if row["title"] == "Учебный пожар: двор"
            )
            payload = scenario["card_payload"]
            title = "Проверка заполнения " + str(int(time.time()))
            lesson = api(
                teacher,
                "/teacher/lessons",
                "POST",
                {
                    "title": title,
                    "participants": [{"student_id": me["id"]}],
                    "settings": {
                        "training_mode": "CARD_ENTRY",
                        "primary_status_deadline_sec": 3600,
                        "card_processing_deadline_sec": 3600,
                        "grammar_check_enabled": False,
                    },
                },
            )
            lesson_id = lesson["id"]
            assigned = api(
                teacher,
                f"/teacher/lessons/{lesson_id}/assign",
                "POST",
                {
                    "assignments": [
                        {"student_id": me["id"], "scenario_id": scenario["id"]}
                    ],
                },
            )
            card_id = assigned["assignment_ids"][0]
            api(teacher, f"/teacher/lessons/{lesson_id}/start", "POST")
            running = True
            queue = student.get_by_role("navigation", name="Активные", exact=True)
            expect(queue.get_by_role("button").first).to_be_visible()
            queue.get_by_role("button").first.click()
            name = student.get_by_label("Имя заявителя", exact=True)
            expect(name).to_have_value("")
            name.fill(payload["applicant"]["name"])
            student.reload()
            queue.get_by_role("button").first.click()
            expect(name).to_have_value(payload["applicant"]["name"])
            student.get_by_label("Контактный телефон", exact=True).fill(
                payload["applicant"]["phone"]
            )
            student.get_by_label("Адрес", exact=True).fill(payload["address"]["raw"])
            student.get_by_label(
                "Уточнение адреса, корпус, подъезд, этаж", exact=True
            ).fill(payload["address"].get("clarification") or "")
            student.get_by_label("Описание", exact=True).fill(payload["description"])
            incident = api(
                teacher, f"/student/entry-types/{scenario['incident_type_id']}"
            )
            student.get_by_label("Группа происшествий", exact=True).select_option(
                incident["group_id"]
            )
            student.get_by_label("Тип происшествия", exact=True).select_option(
                incident["id"]
            )
            directory = api(student, "/student/entry-directory")
            for modifier in directory["modifiers"]:
                if modifier["code"] in payload.get("modifiers", []):
                    student.get_by_label(modifier["label"], exact=True).check()
            for service in directory["services"]:
                if service["code"] in payload["notified_services"]:
                    student.get_by_label(service["name"], exact=True).check()
            layouts(student, "entry-form", cases)
            before = student.get_by_test_id("processing-timer").inner_text()
            student_context.set_offline(True)
            student.get_by_role("button", name="Сохранить черновик", exact=True).click()
            student.wait_for_timeout(30000)
            assert student.get_by_test_id("processing-timer").inner_text() != before
            student_context.set_offline(False)
            retry = student.get_by_role("button", name="Повторить сейчас").first
            if retry.is_visible():
                retry.click()
            expect(
                student.get_by_text("Черновик сохранён на сервере.", exact=True)
            ).to_be_visible(timeout=30000)
            state = api(student, f"/student/assignments/{card_id}")
            assert state["entry"]["revision"] == 1
            assert (
                state["entry"]["draft"]["address"]["raw"] == payload["address"]["raw"]
            )
            student.get_by_role("button", name="Сдать карточку", exact=True).click()
            expect(
                student.get_by_role("button", name="Вернуться", exact=True)
            ).to_be_focused()
            student.keyboard.press("Escape")
            expect(
                student.get_by_role("button", name="Сдать карточку", exact=True)
            ).to_be_focused()
            student.get_by_role("button", name="Сдать карточку", exact=True).click()
            student.get_by_role("dialog").get_by_role(
                "button", name="Сдать карточку", exact=True
            ).click()
            expect(
                student.get_by_role(
                    "heading", name="Сравнение с эталоном · 100 / 100", exact=True
                )
            ).to_be_visible(timeout=30000)
            layouts(student, "entry-result", cases)
            submitted = api(student, f"/student/assignments/{card_id}")
            assert submitted["entry"]["score"]["total"] == 100
            assert submitted["my_block"]["available_statuses"] == []
            teacher.reload()
            row = teacher.get_by_role("row").filter(has_text=title)
            row.get_by_role("button", name="Открыть пульт", exact=True).click()
            teacher.get_by_role(
                "table", name="Участники занятия", exact=True
            ).get_by_role("button").first.click()
            teacher.get_by_role(
                "button", name="Подготовить сценарий из этой карточки", exact=True
            ).click()
            expect(
                teacher.get_by_text(
                    "Сценарий создан и ожидает вашей проверки в библиотеке подготовки.",
                    exact=True,
                )
            ).to_be_visible()
            layouts(teacher, "entry-observation", cases, [1440, 390])
            api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            running = False
            expect(
                student.get_by_text("Занятие начнёт преподаватель.", exact=True)
            ).to_be_visible()
            expect(
                student.get_by_role(
                    "heading", name="Сравнение с эталоном · 100 / 100", exact=True
                )
            ).to_be_visible()
            reviews = api(
                teacher, "/teacher/scenarios?status=PENDING_REVIEW&limit=200"
            )["items"]
            reused = next(
                row
                for row in reviews
                if row["card_payload"].get("source_assignment_id") == card_id
            )
            continuation = api(
                teacher,
                "/teacher/lessons",
                "POST",
                {
                    "title": title + " · действия",
                    "participants": [{"student_id": me["id"]}],
                    "settings": {
                        "primary_status_deadline_sec": 3600,
                        "card_processing_deadline_sec": 3600,
                    },
                },
            )
            lesson_id = continuation["id"]
            teacher.reload()
            row = teacher.get_by_role("row").filter(has_text=title + " · действия")
            row.get_by_role("button", name="Подготовить", exact=True).click()
            teacher.get_by_label(
                "Показать также импортированные билеты и другие сценарии", exact=True
            ).check()
            teacher.get_by_label("Сценарий на проверке", exact=True).select_option(
                reused["id"]
            )
            layouts(teacher, "entry-reuse-review", cases, [1440, 390])
            teacher.get_by_role("button", name="Подтвердить", exact=True).click()
            expect(
                teacher.get_by_role("heading", name=reused["title"], exact=True)
            ).to_have_count(0)
            api(
                teacher,
                f"/teacher/lessons/{lesson_id}/assign",
                "POST",
                {
                    "assignments": [
                        {"student_id": me["id"], "scenario_id": reused["id"]}
                    ]
                },
            )
            api(teacher, f"/teacher/lessons/{lesson_id}/start", "POST")
            running = True
            expect(queue.get_by_role("button").first).to_be_visible()
            queue.get_by_role("button").first.click()
            for status in [
                "Принята",
                "Реагирование начато",
                "Прибыли",
                "Работы ведутся",
                "Работы завершены 🔒",
            ]:
                student.get_by_role("button", name=status, exact=True).click()
                student.get_by_role(
                    "button", name="Сохранить статус", exact=True
                ).click()
                if "🔒" in status:
                    student.get_by_role(
                        "button", name="Закрыть карточку", exact=True
                    ).click()
                else:
                    expect(
                        student.get_by_role("button", name=status, exact=True)
                    ).to_have_count(0)
            expect(
                student.get_by_text("Карточка закрыта", exact=True).first
            ).to_be_visible()
            api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            running = False
            assert not errors, errors
            passed = True
        finally:
            student_context.set_offline(False)
            if running:
                api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            (OUTPUT / "card-entry.json").write_text(
                json.dumps(
                    {
                        "passed": passed,
                        "cases": cases,
                        "page_errors": errors,
                        "lesson_id": lesson_id,
                        "offline_seconds": 30,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            browser.close()
    print(
        f"PASS entry: {len(cases)} layouts; local draft restored after reload, 30s offline, deterministic 100, teacher reuse",
        flush=True,
    )


if __name__ == "__main__":
    main()
