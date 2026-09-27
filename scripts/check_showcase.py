"""Rehearse the two prepared presentation lessons through the real browser UI."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, context_options, launch_browser
from check_responsive import api

OUTPUT = Path(__file__).resolve().parents[1] / "artifacts/ui-review/presentation"


def capture(page, screen, results):
    for width in (1440, 768, 390, 360):
        page.set_viewport_size({"width": width, "height": 960 if width > 700 else 844})
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(100)
        measured = page.evaluate("document.documentElement.scrollWidth")
        assert measured <= width + 1, (screen, width, measured)
        results.append({"screen": screen, "width": width, "document": measured})
        if width in (1440, 390):
            page.screenshot(path=str(OUTPUT / f"{screen}-{width}.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 960})
    print(f"PASS {screen}: four widths", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results, errors, scores = [], [], {}
    active = None
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        teacher, student = [
            browser.new_context(
                viewport={"width": 1440, "height": 960}, **context_options()
            ).new_page()
            for _ in range(2)
        ]
        for page in (teacher, student):
            page.set_default_timeout(20000)
            page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            for role, page in (("teacher", teacher), ("student", student)):
                page.goto(f"{BASE_URL}/?showcase={role}")
                expect(page.get_by_label("Логин", exact=True)).to_have_value(
                    f"demo.{role}"
                )
                if role == "teacher":
                    capture(page, "login", results)
                page.get_by_role(
                    "button", name="Вход в учебную систему", exact=True
                ).click()
                expect(
                    page.get_by_role("button", name="Выйти", exact=True)
                ).to_be_visible()
            assert api(student, "/student/state")["lesson"] is None, (
                "Leave an existing lesson untouched"
            )
            lessons = api(teacher, "/teacher/lessons")["items"]
            prepared = [row for row in lessons if row["status"] == "PLANNED"]
            assert len(prepared) == 2, (
                "Prepare two lessons with scripts/present.py --prepare-only first"
            )
            capture(teacher, "lessons", results)
            capture(student, "waiting", results)
            if args.layout_only:
                teacher.get_by_label("Показать занятия", exact=True).select_option(
                    "PLANNED"
                )
                expect(
                    teacher.get_by_role("button", name="Открыть пульт", exact=True)
                ).to_have_count(2)
                capture(teacher, "polished-lessons", results)
                for width in (360, 390, 768, 1440):
                    teacher.set_viewport_size({"width": width, "height": 960})
                    for button in teacher.get_by_role(
                        "button", name="Открыть пульт", exact=True
                    ).all():
                        box = button.bounding_box()
                        assert (
                            box and box["x"] >= 0 and box["x"] + box["width"] <= width
                        )
                assert not any(row["status"] == "RUNNING" for row in lessons)
                assert not errors, errors
                print("PASS prepared lessons, filters and mobile actions", flush=True)
                return
            scenarios = api(teacher, "/teacher/scenarios?status=APPROVED&limit=200")[
                "items"
            ]
            for mode in ("CARD_ACTIONS", "CARD_ENTRY"):
                lesson = next(
                    row for row in prepared if row["settings"]["training_mode"] == mode
                )
                teacher.locator(f'tr[data-lesson-id="{lesson["id"]}"]').get_by_role(
                    "button", name="Открыть пульт", exact=True
                ).click()
                teacher.get_by_role(
                    "button", name="Начать занятие", exact=True
                ).first.click()
                active = lesson["id"]
                queue = student.get_by_role("navigation", name="Активные", exact=True)
                queue.get_by_role("button").first.click()
                if mode == "CARD_ACTIONS":
                    for status in (
                        "Принята · Alt+1",
                        "Реагирование начато",
                        "Прибыли",
                        "Работы ведутся",
                    ):
                        student.get_by_role("button", name=status, exact=True).click()
                        student.get_by_role(
                            "button", name="Сохранить статус", exact=True
                        ).click()
                        expect(
                            student.get_by_role("button", name=status, exact=True)
                        ).to_have_count(0)
                    capture(student, "card", results)
                    student.keyboard.press("Alt+T")
                    student.get_by_label("Номер абонента", exact=True).fill("2201")
                    student.get_by_role("button", name="Вызов", exact=True).click()
                    student.get_by_label("Ваш доклад", exact=True).fill(
                        "Дубнинская улица, дом 28. Пожар в частном доме. Пострадавших нет."
                    )
                    capture(student, "report-call", results)
                    student.get_by_role(
                        "button", name="Завершить доклад", exact=True
                    ).click()
                    expect(
                        student.get_by_label("Номер абонента", exact=True)
                    ).to_be_visible()
                    student.keyboard.press("Escape")
                    student.get_by_role(
                        "button", name="Работы завершены 🔒", exact=True
                    ).click()
                    student.get_by_role(
                        "button", name="Сохранить статус", exact=True
                    ).click()
                    student.get_by_role(
                        "button", name="Закрыть карточку", exact=True
                    ).click()
                    expect(
                        student.get_by_text("Карточка закрыта", exact=True).first
                    ).to_be_visible()
                else:
                    scenario = next(
                        row for row in scenarios if row["title"] == lesson["title"]
                    )
                    card = scenario["card_payload"]
                    for label, value in (
                        ("Имя заявителя", card["applicant"]["name"]),
                        ("Контактный телефон", card["applicant"]["phone"]),
                        ("Адрес", card["address"]["raw"]),
                        (
                            "Уточнение адреса, корпус, подъезд, этаж",
                            card["address"]["clarification"],
                        ),
                        ("Описание", card["description"]),
                    ):
                        student.get_by_label(label, exact=True).fill(value)
                    incident = api(
                        student, f"/student/entry-types/{scenario['incident_type_id']}"
                    )
                    student.get_by_label(
                        "Группа происшествий", exact=True
                    ).select_option(incident["group_id"])
                    student.get_by_label("Тип происшествия", exact=True).select_option(
                        incident["id"]
                    )
                    student.get_by_role(
                        "button",
                        name="Подобрать службы по типу и признакам",
                        exact=True,
                    ).click()
                    directory = api(student, "/student/entry-directory")
                    for service in directory["services"]:
                        if service["code"] in card["notified_services"]:
                            expect(
                                student.get_by_label(service["name"], exact=True)
                            ).to_be_checked()
                    student.get_by_role(
                        "button", name="Сохранить черновик", exact=True
                    ).click()
                    expect(
                        student.get_by_text("Черновик сохранён на сервере.", exact=True)
                    ).to_be_visible()
                    capture(student, "entry", results)
                    student.get_by_role(
                        "button", name="Сдать карточку", exact=True
                    ).click()
                    student.get_by_role("dialog").get_by_role(
                        "button", name="Сдать карточку", exact=True
                    ).click()
                    expect(
                        student.get_by_role(
                            "heading",
                            name="Сравнение с эталоном · 100 / 100",
                            exact=True,
                        )
                    ).to_be_visible(timeout=30000)
                    capture(student, "entry-result", results)
                capture(teacher, f"live-{mode.lower()}", results)
                teacher.get_by_role(
                    "button", name="Завершить занятие", exact=True
                ).first.click()
                expect(
                    student.get_by_text("Занятие начнёт преподаватель.", exact=True)
                ).to_be_visible()
                active = None
                row = teacher.locator(f'tr[data-lesson-id="{lesson["id"]}"]')
                row.get_by_role(
                    "button", name="Отчёт о практическом занятии", exact=True
                ).click()
                expect(
                    teacher.get_by_role("button", name="Скачать PDF", exact=True)
                ).to_be_visible()
                capture(teacher, f"result-{mode.lower()}", results)
                for label, extension in (
                    ("Скачать PDF", "pdf"),
                    ("Скачать Excel", "xlsx"),
                ):
                    with teacher.expect_download() as download:
                        teacher.get_by_role("button", name=label, exact=True).click()
                    result = download.value
                    assert result.failure() is None
                    result.save_as(OUTPUT / f"{mode.lower()}.{extension}")
                scores[mode] = api(teacher, f"/teacher/lessons/{lesson['id']}/report")
            assert not errors, errors
            print(
                "PASS both exercises, report downloads and browser errors", flush=True
            )
        finally:
            if active:
                api(teacher, f"/teacher/lessons/{active}/finish", "POST")
            (OUTPUT / ("ready.json" if args.layout_only else "result.json")).write_text(
                json.dumps(
                    {"layouts": results, "errors": errors, "reports": scores},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            browser.close()


if __name__ == "__main__":
    main()
