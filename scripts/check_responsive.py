"""Real browser layout/interaction regression against seeded local Compose.

Uses the public demo accounts and a separate, audited test lesson. Existing
lessons are not started or stopped. The test lesson is finished even on failure.
No API mocks: screenshots contain the actual application's data.
"""

import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from browser_environment import BASE_URL, context_options, launch_browser

OUTPUT = Path(
    os.environ.get(
        "DISPATCHER_ARTIFACTS",
        Path(__file__).resolve().parents[1] / "artifacts" / "ui-review",
    )
)
if os.environ.get("DISPATCHER_BROWSER"):
    OUTPUT /= os.environ["DISPATCHER_BROWSER"]
WIDTHS = [1920, 1440, 1280, 1024, 768, 390, 360]
OUTPUT.mkdir(parents=True, exist_ok=True)


def api(page, path, method="GET", body=None):
    return page.evaluate(
        """async ({path, method, body}) => {
          const me = await (await fetch('/api/v1/auth/me', {signal: AbortSignal.timeout(15000)})).json();
          const response = await fetch('/api/v1' + path, {
            method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf_token},
            signal: AbortSignal.timeout(15000),
            ...(body === null ? {} : {body: JSON.stringify(body)})
          });
          const result = await response.json();
          if (!response.ok) throw new Error(JSON.stringify({path, status: response.status, result}));
          return result;
        }""",
        {"path": path, "method": method, "body": body},
    )


def login(page, name, password):
    if os.environ.get("DISPATCHER_BROWSER_USERS"):
        names = json.loads(
            Path(os.environ["DISPATCHER_BROWSER_USERS"]).read_text(encoding="utf-8")
        )
        name = names.get(name, name)
    page.goto(BASE_URL)
    page.get_by_label("Логин", exact=True).fill(name)
    page.get_by_label("Пароль", exact=True).fill(password)
    page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
    expect(page.get_by_role("button", name="Выйти", exact=True)).to_be_visible()


def layouts(page, name, cases, widths=WIDTHS):
    for width in widths:
        page.set_viewport_size({"width": width, "height": 900 if width >= 700 else 844})
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(100)
        measurements = page.evaluate(
            """() => ({
              viewport: innerWidth, document: document.documentElement.scrollWidth,
              training: [...document.querySelectorAll('span')].some(e => e.textContent === 'Учебный режим' && e.getBoundingClientRect().width > 0),
              smallButtons: [...document.querySelectorAll('button')].filter(e => {
                const r = e.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && (r.width < 39.5 || r.height < 39.5);
              }).map(e => e.textContent)
            })"""
        )
        assert measurements["document"] <= width + 1, (name, width, measurements)
        assert measurements["training"], (name, "training label hidden")
        assert not measurements["smallButtons"], (
            name,
            width,
            measurements["smallButtons"],
        )
        cases.append({"screen": name, **measurements})
        if width in (1440, 390):
            page.screenshot(path=str(OUTPUT / f"{name}-{width}.png"), full_page=False)
        print(f"PASS layout {name} {width}", flush=True)


def main():
    cases, errors = [], []
    passed = False
    lesson_id = None
    started = False
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        contexts = [
            browser.new_context(
                viewport={"width": 1440, "height": 900}, **context_options()
            )
            for _ in range(3)
        ]
        teacher, student, admin = [context.new_page() for context in contexts]
        for page in (teacher, student, admin):
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_default_timeout(15000)
        try:
            teacher.goto(BASE_URL)
            expect(teacher.get_by_label("Логин", exact=True)).to_be_visible()
            layouts(teacher, "login", cases)
            teacher.set_viewport_size({"width": 1440, "height": 900})
            login(teacher, "teacher", "teacher")
            login(student, "student1", "student")
            state = api(student, "/student/state")
            assert state["lesson"] is None, (
                "student1 already has an active lesson; leave it untouched"
            )
            expect(
                student.get_by_text("Занятие начнёт преподаватель.", exact=True)
            ).to_be_visible()
            layouts(student, "waiting", cases)
            expect(
                teacher.get_by_role("heading", name="Занятия", exact=True)
            ).to_be_visible()
            layouts(teacher, "lessons", cases)
            teacher.get_by_text("Новое занятие", exact=True).click()
            expect(teacher.get_by_label("Название", exact=True)).to_be_visible()
            layouts(teacher, "lesson-form", cases)
            teacher.get_by_text("Новое занятие", exact=True).click()

            me = api(student, "/auth/me")
            scenarios = api(teacher, "/teacher/scenarios?status=APPROVED&limit=200")[
                "items"
            ]
            scenario = next(
                row for row in scenarios if row["title"] == "Учебный пожар: двор"
            )
            title = "Проверка адаптивности " + str(int(time.time()))
            lesson = api(
                teacher,
                "/teacher/lessons",
                "POST",
                {
                    "title": title,
                    "participants": [{"student_id": me["id"]}],
                    "settings": {
                        "primary_status_deadline_sec": 3600,
                        "card_processing_deadline_sec": 3600,
                    },
                },
            )
            lesson_id = lesson["id"]
            teacher.reload()
            teacher.get_by_role("row").filter(has_text=title).get_by_role(
                "button", name="Подготовить", exact=True
            ).click()
            expect(
                teacher.get_by_role("heading", name="Подготовка занятия", exact=True)
            ).to_be_visible()
            teacher.get_by_label("Количество сценариев", exact=True).fill("1")
            teacher.get_by_label("Способ подготовки", exact=True).select_option(
                "template"
            )
            teacher.get_by_role(
                "button", name="Сгенерировать сценарии", exact=True
            ).click()
            expect(
                teacher.get_by_role("heading", name="Эталонное решение", exact=True)
            ).to_be_visible(timeout=30000)
            layouts(teacher, "preparation", cases)
            teacher.get_by_role("button", name="Закрыть панель", exact=True).click()
            api(
                teacher,
                f"/teacher/lessons/{lesson_id}/assign",
                "POST",
                {
                    "assignments": [
                        {"student_id": me["id"], "scenario_id": scenario["id"]}
                    ],
                },
            )
            api(teacher, f"/teacher/lessons/{lesson_id}/start", "POST")
            started = True
            queue = student.get_by_role("navigation", name="Активные", exact=True)
            expect(queue.get_by_role("button").first).to_be_visible()
            layouts(student, "queue", cases, [1280, 768, 390, 360])
            queue.get_by_role("button").first.click()
            expect(
                student.get_by_role("button", name="Принята", exact=True)
            ).to_be_visible()
            student.keyboard.press("Alt+2")
            comment = student.get_by_role("textbox")
            expect(comment).to_be_focused()
            comment.fill("Коротко")
            expect(
                student.get_by_role("button", name="Сохранить статус", exact=True)
            ).to_be_disabled()
            comment.fill("Уточнённый адрес: Чертановская улица, дом 9.")
            for width in WIDTHS:
                layouts(student, "card", cases, [width])
                expect(comment).to_have_value(
                    "Уточнённый адрес: Чертановская улица, дом 9."
                )
                expect(student.get_by_test_id("primary-timer")).to_be_visible()
                expect(student.get_by_test_id("processing-timer")).to_be_visible()
                if width <= 1024:
                    student.get_by_role("link", name="Действия", exact=True).click()
                    expect(
                        student.get_by_role(
                            "button", name="Сохранить статус", exact=True
                        )
                    ).to_be_in_viewport()
            student.keyboard.press("Alt+T")
            expect(
                student.get_by_role("button", name="Закрыть телефонию", exact=True)
            ).to_be_visible()
            layouts(student, "phone", cases)
            student.keyboard.press("Escape")
            expect(
                student.get_by_role("button", name="Телефония · Alt+T", exact=True)
            ).to_be_focused()
            student.keyboard.press("Alt+1")
            student.get_by_role("button", name="Сохранить статус", exact=True).click()
            expect(
                student.get_by_role("button", name="Реагирование начато", exact=True)
            ).to_be_visible()

            before = student.get_by_test_id("processing-timer").inner_text()
            contexts[1].set_offline(True)
            student.get_by_role(
                "button", name="Реагирование начато", exact=True
            ).click()
            student.get_by_role("button", name="Сохранить статус", exact=True).click()
            student.wait_for_timeout(30000)
            assert student.get_by_test_id("processing-timer").inner_text() != before
            layouts(student, "offline", cases, [1280, 390, 360])
            contexts[1].set_offline(False)
            expect(
                student.get_by_role("button", name="Прибыли", exact=True)
            ).to_be_enabled(timeout=30000)
            for status in ("Прибыли", "Работы ведутся"):
                student.get_by_role("button", name=status, exact=True).click()
                student.get_by_role(
                    "button", name="Сохранить статус", exact=True
                ).click()
                expect(
                    student.get_by_role("button", name=status, exact=True)
                ).to_have_count(0)
            student.get_by_role(
                "button", name="Работы завершены 🔒", exact=True
            ).click()
            student.get_by_role("button", name="Сохранить статус", exact=True).click()
            expect(
                student.get_by_role("button", name="Вернуться", exact=True)
            ).to_be_focused()
            layouts(student, "confirmation", cases, [1440, 390, 360])
            student.keyboard.press("Escape")
            expect(student.get_by_role("dialog")).not_to_be_visible()
            student.get_by_role("button", name="Сохранить статус", exact=True).click()
            student.get_by_role("button", name="Закрыть карточку", exact=True).click()
            expect(
                student.get_by_text("Карточка закрыта", exact=True).first
            ).to_be_visible()
            card_id = api(student, "/student/state")["cards"][0]["assignment_id"]
            detail = api(student, f"/student/assignments/{card_id}")
            statuses = [
                row["status"]
                for row in detail["my_block"]["history"]
                if not row["is_automatic"]
            ]
            assert statuses == [
                "ACCEPTED",
                "RESPONSE_STARTED",
                "ARRIVED",
                "WORK_IN_PROGRESS",
                "WORK_COMPLETED",
            ], statuses

            teacher.reload()
            row = teacher.get_by_role("row").filter(has_text=title)
            row.get_by_role("button", name="Открыть пульт", exact=True).click()
            expect(
                teacher.get_by_role("table", name="Участники занятия", exact=True)
            ).to_be_visible()
            layouts(teacher, "teacher-live", cases)
            teacher.get_by_role(
                "table", name="Участники занятия", exact=True
            ).get_by_role("button").first.click()
            expect(
                teacher.get_by_role("heading", name="Корректировка оценки", exact=True)
            ).to_be_visible()
            layouts(teacher, "observation", cases)
            teacher.get_by_role("button", name="Закрыть наблюдение", exact=True).click()
            api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            started = False
            expect(
                student.get_by_text("Занятие начнёт преподаватель.", exact=True)
            ).to_be_visible()
            layouts(student, "results", cases)
            row.get_by_role(
                "button", name="Отчёт о практическом занятии", exact=True
            ).click()
            expect(
                teacher.get_by_role("button", name="Скачать PDF", exact=True)
            ).to_be_visible()
            layouts(teacher, "report", cases)
            with teacher.expect_download() as download:
                teacher.get_by_role("button", name="Скачать PDF", exact=True).click()
            report = download.value
            assert report.failure() is None, report.failure()
            report.save_as(OUTPUT / "responsive-report.pdf")
            assert (OUTPUT / "responsive-report.pdf").read_bytes().startswith(b"%PDF-")

            login(admin, "admin", "admin")
            nav = admin.get_by_role("navigation", name="Администрирование", exact=True)
            for name in [
                "Пользователи",
                "Рабочие места",
                "Классификатор",
                "Справочник улиц",
                "Журнал аудита",
                "Состояние системы",
            ]:
                if admin.get_by_label(
                    "Раздел администрирования", exact=True
                ).is_visible():
                    admin.get_by_label(
                        "Раздел администрирования", exact=True
                    ).select_option(label=name)
                else:
                    nav.get_by_role("button", name=name, exact=True).click()
                expect(
                    admin.get_by_role("heading", name=name, exact=True)
                ).to_be_visible()
                expect(admin.get_by_role("table").first).to_be_visible()
                layouts(admin, "admin-" + str(len(cases)), cases, [1440, 768, 390, 360])
            assert not errors, errors
            passed = True
        finally:
            if started:
                api(teacher, f"/teacher/lessons/{lesson_id}/finish", "POST")
            (OUTPUT / "responsive.json").write_text(
                json.dumps(
                    {
                        "passed": passed,
                        "cases": cases,
                        "page_errors": errors,
                        "lesson_id": lesson_id,
                        "widths": WIDTHS,
                        "offline_seconds": 30,
                        "browser": os.environ.get("DISPATCHER_BROWSER", "chromium"),
                        "browser_version": browser.version,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            browser.close()
    print(
        f"PASS: {len(cases)} layouts, real status cycle, draft across resize, modal/phone focus, 30s offline",
        flush=True,
    )


if __name__ == "__main__":
    main()
