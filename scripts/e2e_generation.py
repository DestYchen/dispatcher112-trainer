"""Review and regenerate new scenarios through the real browser and worker."""

import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_environment import browser_args
from check_responsive import OUTPUT, api, login


def wait_job(page, identity):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = api(page, "/teacher/jobs/" + identity)
        if job["status"] in ("DONE", "FAILED"):
            assert job["status"] == "DONE" and job["rejected"] == 0, job
            return job
        page.wait_for_timeout(250)
    raise AssertionError("Generation did not complete within 120 seconds")


def main():
    result = {"passed": False, "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        try:
            login(page, "teacher", "teacher")
            student = api(page, "/teacher/participants")["students"][0]["id"]
            title = "Проверка рецензирования " + str(time.time_ns())
            lesson = api(
                page,
                "/teacher/lessons",
                "POST",
                {
                    "title": title,
                    "participants": [{"student_id": student}],
                },
            )
            result["lesson_id"] = lesson["id"]
            queued = api(
                page,
                f"/teacher/lessons/{lesson['id']}/scenarios/generate",
                "POST",
                {
                    "count": 2,
                    "generation_backend": "template",
                },
            )
            assert wait_job(page, queued["job_id"])["generated"] == 2
            page.reload()
            page.get_by_role("row").filter(has_text=title).get_by_role(
                "button",
                name="Подготовить",
                exact=True,
            ).click()
            selector = page.get_by_label("Сценарий на проверке", exact=True)
            expect(selector).to_be_visible()
            approved_id = selector.input_value()
            rationale = "Эталон проверен преподавателем по условиям карточки."
            page.get_by_label("Обоснование решения", exact=True).fill(rationale)
            page.get_by_label("Замечание для перегенерации", exact=True).fill(
                "Эталон проверен."
            )
            page.get_by_role("button", name="Подтвердить", exact=True).click()
            expect(selector).not_to_have_value(approved_id)
            approved = api(
                page, f"/teacher/scenarios?status=APPROVED&lesson_id={lesson['id']}"
            )["items"]
            assert (
                next(row for row in approved if row["id"] == approved_id)["reference"][
                    "rationale"
                ]
                == rationale
            )
            rejected_id = selector.input_value()
            comment = "Добавьте уточнение показаний заявителя."
            page.get_by_label("Замечание для перегенерации", exact=True).fill(comment)
            with page.expect_response(
                lambda response: response.url.endswith("/reject")
            ) as response:
                page.get_by_role(
                    "button", name="Отклонить и перегенерировать", exact=True
                ).click()
            assert response.value.ok, response.value.status
            rejected = response.value.json()
            assert rejected["id"] == rejected_id and rejected["job_id"]
            assert wait_job(page, rejected["job_id"])["generated"] == 1
            feedback = [
                json.loads(line)
                for line in (
                    Path(__file__).resolve().parents[1] / "data/feedback.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            matches = [row for row in feedback if row["scenario_id"] == rejected_id]
            assert len(matches) == 1 and matches[0]["comment"] == comment
            result.update(
                approved_id=approved_id, rejected_id=rejected_id, feedback_entries=1
            )
            page.screenshot(path=str(OUTPUT / "runtime-review.png"), full_page=True)
            assert not result["errors"], result["errors"]
            result["passed"] = True
        finally:
            (OUTPUT / "runtime-review.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            browser.close()
    print("PASS: approval, persisted feedback and real worker regeneration", flush=True)


if __name__ == "__main__":
    main()
