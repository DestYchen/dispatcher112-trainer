"""Real administrative reporting, persistent API failure, restart and full download."""

import json
import subprocess
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_environment import BASE_URL, browser_args
from check_responsive import OUTPUT, api, layouts, login


def main():
    result = {
        "passed": False,
        "mode": "real API, database and backend restart",
        "layouts": [],
        "errors": [],
    }
    enabled = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        try:
            login(page, "admin", "admin")
            before = api(page, "/admin/maintenance")
            assert not before["enabled"] and not before.get("job_id"), (
                "Preserve existing maintenance"
            )
            initial = api(page, "/admin/diagnostics/report")
            assert all(row["status"] == "OK" for row in initial["integrity"]), initial[
                "integrity"
            ]
            api(
                page,
                "/admin/maintenance",
                "POST",
                {
                    "enabled": True,
                    "reason": "Приёмка технического отчёта и сохранности журнала",
                },
            )
            enabled = True
            # Read-only request to the deliberately disabled controller. No service command.
            probe = page.evaluate("""async () => {
                const response = await fetch('/api/v1/admin/operations/services');
                const body = await response.json();
                return {status: response.status, request_id: response.headers.get('x-request-id'), code: body.error?.code};
            }""")
            assert probe["status"] == 503 and probe["code"] == "CONTROL_UNAVAILABLE", (
                probe
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                report = api(page, "/admin/diagnostics/report")
                matching = [
                    row
                    for row in report["failures"]["items"]
                    if row.get("request_id") == probe["request_id"]
                ]
                if matching:
                    break
                page.wait_for_timeout(200)
            else:
                raise AssertionError("Actual API failure was not persisted")
            assert len(matching) == 1 and matching[0]["code"] == "CONTROL_UNAVAILABLE"
            print(
                "PASS: actual 503 saved once with request ID and canonical route",
                flush=True,
            )
            subprocess.run(
                ["docker", "compose", "restart", "backend"], check=True, timeout=60
            )
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "up",
                    "--detach",
                    "--no-deps",
                    "--wait",
                    "backend",
                ],
                check=True,
                timeout=60,
            )
            page.goto(BASE_URL + "/admin")
            after = api(page, "/admin/diagnostics/report")
            assert all(row["status"] == "OK" for row in after["integrity"]), after[
                "integrity"
            ]
            assert after["http"]["since"] != initial["http"]["since"]
            assert any(
                row.get("request_id") == probe["request_id"]
                for row in after["failures"]["items"]
            )
            assert not after["http"]["journal_write_failures"]
            result["persisted_across_backend_restart"] = True
            result["request_id"] = probe["request_id"]
            result["integrity"] = after["integrity"]
            page.get_by_role("button", name="Технический отчёт", exact=True).click()
            expect(page.get_by_text(probe["request_id"], exact=True)).to_be_visible()
            layouts(page, "diagnostics-live", result["layouts"])
            with page.expect_download() as pending:
                page.get_by_role(
                    "button", name="Скачать полный отчёт JSON", exact=True
                ).click()
            downloaded = json.loads(
                Path(pending.value.path()).read_text(encoding="utf-8")
            )
            assert (
                len(downloaded["failures"]["items"]) == downloaded["failures"]["total"]
            )
            assert downloaded["failures"]["next_cursor"] is None
            assert any(
                row.get("request_id") == probe["request_id"]
                for row in downloaded["failures"]["items"]
            )
            result["exported_failures"] = downloaded["failures"]["total"]
            result["inventory"] = after["inventory"]
            assert not result["errors"], result["errors"]
            result["passed"] = True
            print(
                "PASS: persistent failure survives backend restart; seven layouts and full JSON download",
                flush=True,
            )
        except Exception as error:
            result["failure"] = str(error)
            raise
        finally:
            try:
                if enabled:
                    api(
                        page,
                        "/admin/maintenance",
                        "POST",
                        {
                            "enabled": False,
                            "reason": "Приёмка технического отчёта завершена",
                        },
                    )
            except Exception as error:
                result.update(passed=False, cleanup_error=str(error))
                raise
            finally:
                (OUTPUT / "diagnostics-live.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                browser.close()


if __name__ == "__main__":
    main()
