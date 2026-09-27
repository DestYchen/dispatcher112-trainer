"""Actual local model inference through the lesson API, Redis worker and TLS proxy."""

import json
import time

from playwright.sync_api import sync_playwright

from browser_environment import browser_args
from check_responsive import OUTPUT, api, login


def main():
    result = {"passed": False, "backend": "local_llm", "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page()
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        try:
            login(page, "teacher", "teacher")
            student = api(page, "/teacher/participants")["students"][0]["id"]
            lesson = api(
                page,
                "/teacher/lessons",
                "POST",
                {
                    "title": "Проверка локальной модели " + str(time.time_ns()),
                    "participants": [{"student_id": student}],
                },
            )
            result["lesson_id"] = lesson["id"]
            started = time.monotonic()
            queued = api(
                page,
                f"/teacher/lessons/{lesson['id']}/scenarios/generate",
                "POST",
                {
                    "count": 1,
                    "difficulty_range": [1, 3],
                    "generation_backend": "local_llm",
                },
            )
            result["job_id"] = queued["job_id"]
            deadline = started + 180
            while time.monotonic() < deadline:
                job = api(page, "/teacher/jobs/" + queued["job_id"])
                if job["status"] in ("DONE", "FAILED"):
                    break
                page.wait_for_timeout(1000)
            else:
                raise AssertionError(
                    "Generation is still pending; do not submit a duplicate job"
                )
            assert (
                job["status"] == "DONE"
                and job["generated"] == 1
                and job["rejected"] == 0
            ), job
            scenarios = api(
                page,
                f"/teacher/scenarios?status=PENDING_REVIEW&lesson_id={lesson['id']}",
            )["items"]
            assert len(scenarios) == 1
            scenario = scenarios[0]
            payload = scenario["card_payload"]
            assert payload["generation"]["backend"] == "local_llm"
            assert payload["generation"]["model"] == "qwen2.5:3b"
            assert len(payload["generation"]["manifest_sha256"]) == 64
            assert 40 <= len(payload["description"]) <= 400
            assert not result["errors"], result["errors"]
            result.update(
                passed=True,
                elapsed_seconds=round(time.monotonic() - started, 3),
                scenario=scenario,
            )
            print(
                "PASS: actual local model prepared a reviewable scenario through the worker",
                flush=True,
            )
        finally:
            (OUTPUT / "local-generation-runtime.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            browser.close()


if __name__ == "__main__":
    main()
