"""Real local SIP/WebRTC browser exercise; never dials an external number."""

import json
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
from check_responsive import OUTPUT, login


def main():
    result = {"passed": False, "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ])
        context = browser.new_context(permissions=["microphone"])
        context.add_init_script("""window.sipTestConnections = [];
          const NativeConnection = window.RTCPeerConnection;
          window.RTCPeerConnection = class extends NativeConnection {
            constructor(...args) { super(...args); window.sipTestConnections.push(this); }
          };""")
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        try:
            fixture = Path("data/sip-acceptance.json")
            name = json.loads(fixture.read_text(encoding="utf-8"))["participants"][0]["login"] if fixture.exists() else "student1"
            login(page, name, "student")
            page.get_by_role("button", name="Включить гарнитуру", exact=True).click()
            expect(page.get_by_text("Телефон готов", exact=True)).to_be_visible()
            page.get_by_role("button", name="Проверить звук", exact=True).click()
            expect(page.get_by_text("Проверка звука · ваш голос возвращается в гарнитуру", exact=True)).to_be_visible()
            page.wait_for_timeout(5000)
            result["media"] = page.evaluate("""async () => {
                const pc = window.sipTestConnections.at(-1);
                const stats = await pc.getStats();
                return {state: pc.connectionState, ice: pc.iceConnectionState,
                  tracks: [...stats.values()].filter(s => ['inbound-rtp','outbound-rtp','candidate-pair'].includes(s.type))};
            }""")
            (OUTPUT / "sip-diagnostic.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            assert result["media"]["state"] == "connected", result["media"]
            inbound = [row for row in result["media"]["tracks"] if row["type"] == "inbound-rtp"]
            assert inbound and inbound[0]["packetsReceived"] > 50, result["media"]
            result["latency"] = page.evaluate(Path("scripts/sip_media_probe.js").read_text(encoding="utf-8"))
            delays = result["latency"]["delays_ms"]
            assert len(delays) == 12 and all(value is not None and 0 < value <= 150 for value in delays), result["latency"]
            page.get_by_role("button", name="Завершить вызов", exact=True).click()
            expect(page.get_by_text("Нет активного вызова", exact=True)).to_be_visible()
            result["passed"] = True
            print(f"PASS: SIP, bidirectional RTP, measured audio round trip max {max(delays):.2f} ms")
        finally:
            page.screenshot(path=str(OUTPUT / "sip-diagnostic.png"))
            (OUTPUT / "sip-diagnostic.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()


if __name__ == "__main__":
    main()
