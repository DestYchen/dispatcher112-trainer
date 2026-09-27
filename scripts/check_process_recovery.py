"""Exercise automatic frontend recovery while the training complex is in maintenance."""

import json
import subprocess
import time

from playwright.sync_api import sync_playwright

from browser_environment import BASE_URL, browser_args
from check_responsive import OUTPUT, api, login


def docker(*arguments):
    completed = subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=30)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def state(identity):
    # Inspect only nonsecret fields, never the container's environment.
    return json.loads(docker("inspect", "--format", '{{json .State}}', identity))


def main():
    result = {"passed": False, "target": "frontend", "policies": {}}
    enabled = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        page = browser.new_page()
        try:
            login(page, "admin", "admin")
            previous = api(page, "/admin/maintenance")
            assert not previous["enabled"] and not previous.get("job_id"), "Preserve existing maintenance"
            records = docker("compose", "ps", "--format", "json")
            rows = [json.loads(line) for line in records.splitlines() if line.strip()]
            for row in rows:
                identity = row["ID"]
                policy = docker("inspect", "--format", '{{.HostConfig.RestartPolicy.Name}}', identity)
                result["policies"][row["Service"]] = policy
                assert policy == "unless-stopped", (row["Service"], policy)
            target = next(row["ID"] for row in rows if row["Service"] == "frontend")
            project = docker("inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', target)
            assert project == "dispatcher112"
            assert state(target)["Health"]["Status"] == "healthy"
            count = int(docker("inspect", "--format", '{{.RestartCount}}', target))
            api(page, "/admin/maintenance", "POST", {"enabled": True, "reason": "Проверка автоматического восстановления интерфейса"})
            enabled = True
            started = time.monotonic()
            # Docker kill/stop are administrative stops and suppress unless-stopped.
            # Terminate only Vite's child process so npm exits as a real process failure.
            crash = subprocess.run([
                "docker", "exec", target, "node", "-e",
                "const fs=require('node:fs');"
                "const matches=fs.readdirSync('/proc').filter(n=>/^\\d+$/.test(n)).filter(n=>{"
                "try{return fs.readFileSync('/proc/'+n+'/cmdline','utf8').split('\\0')"
                ".some(arg=>arg.endsWith('/node_modules/.bin/vite'));}catch{return false;}});"
                "if(matches.length!==1)throw new Error('Exactly one Vite process required');"
                "process.kill(Number(matches[0]),'SIGKILL');",
            ], text=True, capture_output=True, timeout=30)
            if crash.returncode not in (0, 137):
                raise RuntimeError(crash.stderr.strip())
            deadline = started + 90
            while time.monotonic() < deadline:
                current = state(target)
                after = int(docker("inspect", "--format", '{{.RestartCount}}', target))
                if after > count and current.get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(1)
            else:
                raise AssertionError("Frontend did not automatically recover within 90 seconds")
            page.goto(BASE_URL + "/admin")
            assert api(page, "/auth/me")["role"] == "ADMIN"
            result.update(passed=True, recovery_seconds=round(time.monotonic() - started, 3), restarts_before=count, restarts_after=after)
            print(json.dumps(result), flush=True)
        except Exception as error:
            result["failure"] = str(error)
            raise
        finally:
            try:
                if enabled:
                    api(page, "/admin/maintenance", "POST", {"enabled": False, "reason": "Проверка восстановления интерфейса завершена"})
            except Exception as error:
                result.update(passed=False, cleanup_error=str(error))
                raise
            finally:
                (OUTPUT / "process-recovery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                browser.close()


if __name__ == "__main__":
    main()
