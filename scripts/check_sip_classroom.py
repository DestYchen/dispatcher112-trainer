"""Twenty actual browser softphones, audio delay, incoming calls and saved cards."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect

OUTPUT = Path("artifacts/ui-review")
INSTRUMENT = """window.sipTestConnections = [];
const NativeConnection = window.RTCPeerConnection;
window.RTCPeerConnection = class extends NativeConnection {
  constructor(...args) { super(...args); window.sipTestConnections.push(this); }
};"""


async def api(page, path, method="GET", body=None):
    return await page.evaluate("""async ({path,method,body}) => {
      const me = await (await fetch('/api/v1/auth/me')).json();
      const response = await fetch('/api/v1'+path, {method, headers: {
        'Content-Type':'application/json','X-CSRF-Token':me.csrf_token,
        'Idempotency-Key':crypto.randomUUID()}, ...(body===null?{}:{body:JSON.stringify(body)})});
      const result = await response.json();
      if (!response.ok) throw new Error(JSON.stringify({path,status:response.status,result}));
      return result;
    }""", {"path": path, "method": method, "body": body})


async def login(page, name, password):
    await page.goto("http://localhost:5173")
    await page.get_by_label("Логин", exact=True).fill(name)
    await page.get_by_label("Пароль", exact=True).fill(password)
    await page.get_by_role("button", name="Вход в учебную систему", exact=True).click()
    await expect(page.get_by_role("button", name="Выйти", exact=True)).to_be_visible(timeout=20000)


async def main():
    fixture = json.loads(Path("data/sip-acceptance.json").read_text(encoding="utf-8"))
    result = {"passed": False, "errors": [], "participants": 20, "lesson_id": fixture["lesson_id"]}
    running = False
    async with async_playwright() as playwright:
        launch_args = [
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required", "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
        ]
        browsers = [await playwright.chromium.launch(headless=True, args=launch_args) for _ in range(4)]
        teacher = await browsers[0].new_page()
        pages = []
        try:
            await login(teacher, fixture["teacher"], "teacher")

            async def prepare(person):
                browser = browsers[fixture["participants"].index(person) % len(browsers)]
                context = await browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1000})
                await context.add_init_script(INSTRUMENT)
                page = await context.new_page()
                page.set_default_timeout(25000)
                page.on("pageerror", lambda error: result["errors"].append(str(error)))
                await login(page, person["login"], "student")
                await page.get_by_role("button", name="Включить гарнитуру", exact=True).click()
                await expect(page.get_by_text("Телефон готов", exact=True)).to_be_visible(timeout=20000)
                await page.get_by_role("button", name="Проверить звук", exact=True).click()
                try:
                    await expect(page.get_by_text("Проверка звука · ваш голос возвращается в гарнитуру", exact=True)).to_be_visible(timeout=25000)
                except Exception:
                    result.setdefault("failed_phones", []).append({"login": person["login"], "text": await page.locator('section[aria-label="Учебный IP-телефон"]').inner_text(), "connections": await page.evaluate("window.sipTestConnections.map(pc => ({connection:pc.connectionState,ice:pc.iceConnectionState}))")})
                    await page.screenshot(path=str(OUTPUT / f"sip-failed-{person['login']}.png"))
                    raise
                return page

            pages = await asyncio.gather(*(prepare(person) for person in fixture["participants"]))
            await asyncio.sleep(4)
            probe = Path("scripts/sip_media_probe.js").read_text(encoding="utf-8")
            result["probe_mode"] = "sequential capture with all 20 real SIP media streams active"
            result["latency"] = []
            for index, page in enumerate(pages):
                measurement = await page.evaluate(probe)
                result["latency"].append(measurement)
                valid = [value for value in measurement["delays_ms"] if value is not None]
                print(f"Audio probe {index + 1}/20: {len(valid)} pulses, max {max(valid, default=0):.2f} ms", flush=True)
            (OUTPUT / "sip-classroom.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            delays = [value for row in result["latency"] for value in row["delays_ms"]]
            result["latency_passed"] = len(delays) == 240 and all(value is not None and 0 < value <= 150 for value in delays)
            print(f"20 real SIP calls; 240 audio pulses; max round trip {max(delays):.2f} ms", flush=True)
            await asyncio.gather(*(page.get_by_role("button", name="Завершить вызов", exact=True).click() for page in pages))
            await asyncio.sleep(2)
            await api(teacher, f"/teacher/lessons/{fixture['lesson_id']}/start", "POST")
            running = True

            async def incoming(page, person):
                queue = page.get_by_role("navigation", name="Активные", exact=True)
                await expect(queue.get_by_role("button").first).to_be_visible(timeout=20000)
                await queue.get_by_role("button").first.click()
                await expect(page.get_by_label("Имя заявителя", exact=True)).to_be_disabled()
                before = await api(page, f"/student/assignments/{person['assignment_id']}")
                assert before["entry"]["accepted_delay_ms"] is None
                await expect(page.get_by_role("button", name="Ответить на вызов", exact=True)).to_be_visible(timeout=30000)
                await page.get_by_role("button", name="Ответить на вызов", exact=True).click()
                await expect(page.get_by_label("Имя заявителя", exact=True)).to_be_enabled(timeout=20000)
                await page.get_by_label("Имя заявителя", exact=True).fill("Учебный заявитель")
                await page.get_by_role("button", name="Сохранить черновик", exact=True).click()
                await expect(page.get_by_text("Черновик сохранён на сервере.", exact=True)).to_be_visible(timeout=20000)
                calls = await api(page, f"/sip/assignments/{person['assignment_id']}/calls")
                assert calls["calls"][0]["answered_at"] is not None
                return calls["calls"][0]["id"]

            result["incoming_call_ids"] = await asyncio.gather(*(incoming(page, person) for page, person in zip(pages, fixture["participants"])))
            await asyncio.sleep(5)
            await asyncio.gather(*(page.get_by_role("button", name="Завершить вызов", exact=True).click() for page in pages))
            await asyncio.sleep(3)
            recordings = []
            for page, person in zip(pages, fixture["participants"]):
                calls = await api(page, f"/sip/assignments/{person['assignment_id']}/calls")
                recordings.append(calls["calls"][0]["recording_available"])
            assert all(recordings), recordings
            result["recordings"] = len(recordings)
            assert not result["errors"], result["errors"]
            assert result["latency_passed"], "Audio round trip must pass for every measured pulse"
            result["passed"] = True
            print("PASS: 20 incoming calls, server acceptance, concurrent saved drafts and recordings", flush=True)
        finally:
            if running:
                await api(teacher, f"/teacher/lessons/{fixture['lesson_id']}/finish", "POST")
            if pages:
                await pages[0].screenshot(path=str(OUTPUT / "sip-classroom.png"))
            (OUTPUT / "sip-classroom.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.gather(*(browser.close() for browser in browsers))


if __name__ == "__main__":
    asyncio.run(main())
