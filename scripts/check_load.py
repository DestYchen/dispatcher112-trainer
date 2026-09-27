"""Real HTTP/WS training load. Records all timings, verifies each delivered event."""

import asyncio
import argparse
import json
import math
import subprocess
import ssl
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from browser_environment import BASE_URL, ROOT, browser_args
import compose

fixture = json.loads(Path("data/load-acceptance.json").read_text(encoding="utf-8"))
parser = argparse.ArgumentParser()
parser.add_argument(
    "--think-time",
    type=float,
    default=0,
    help="Seconds between user actions; use 0 for saturation stress testing",
)
parser.add_argument("--max-p95-ms", type=float, default=300)
parser.add_argument("--result", type=Path, default=Path("artifacts/stage11-load.json"))
parser.add_argument("--browser-probe", action="store_true")
args = parser.parse_args()
assert args.think_time >= 0
users = len(fixture["students"])
card_count = sum(len(row["assignments"]) for row in fixture["students"])
tls = (
    ssl.create_default_context(cafile=ROOT / ".secrets/pki/ca.crt")
    if BASE_URL.startswith("https:")
    else True
)
timings = []
by_route = defaultdict(list)


async def request(api, method, path, **kwargs):
    start = time.perf_counter()
    response = await api.request(method, path, **kwargs)
    duration = (time.perf_counter() - start) * 1000
    timings.append(duration)
    operation = (
        "detail"
        if method == "GET" and "/assignments/" in path
        else path.rsplit("/", 1)[-1]
    )
    if operation == "status":
        operation += ":" + kwargs["json"]["status"]
    by_route[operation].append(duration)
    response.raise_for_status()
    return response


async def login(name, password):
    api = httpx.AsyncClient(
        base_url=BASE_URL + "/api/v1",
        timeout=60,
        verify=tls,
        trust_env=False,
        headers={"Origin": BASE_URL},
    )
    pre = await api.get("/auth/me")
    csrf = pre.json()["error"]["details"]["csrf_token"]
    response = await request(
        api,
        "POST",
        "/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"login": name, "password": password},
    )
    api.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return api


def p95(values):
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 2)


async def main():
    teacher = await login(fixture["teacher"], "teacher")
    login_slots = asyncio.Semaphore(5)

    async def prepare_student(row):
        async with login_slots:
            return await login(row["login"], "student")

    students = await asyncio.gather(
        *(prepare_student(row) for row in fixture["students"])
    )
    received = [[] for _ in range(users + 1)]
    queues = [asyncio.Queue() for _ in range(users)]
    sockets, tasks, action_latency = [], [], []
    acting_tasks = []
    latency_by_kind = defaultdict(list)
    ui_timings = []
    pages = []
    playwright = browser = None
    if args.browser_probe:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(args=browser_args())
        for student in fixture["students"][:5]:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(BASE_URL)
            await page.get_by_label("Логин", exact=True).fill(student["login"])
            await page.get_by_label("Пароль", exact=True).fill("student")
            await page.get_by_role(
                "button", name="Вход в учебную систему", exact=True
            ).click()
            await page.get_by_role("button", name="Выйти", exact=True).wait_for()
            pages.append(page)
    running = True
    opened_in_browser = [asyncio.Event() for _ in pages]

    async def listen(socket, index):
        async for raw in socket:
            event = json.loads(raw)
            received[index].append(event)
            if index == 0 and event["type"] == "STUDENT_ACTION":
                latency = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(event["ts"])
                ).total_seconds()
                action_latency.append(latency)
                latency_by_kind[event["payload"]["kind"]].append(latency)
            if index and event["type"] == "CARD_DELIVERED":
                await queues[index - 1].put(event["payload"]["assignment_id"])

    async def live_console():
        while running:
            await request(
                teacher, "GET", f"/teacher/lessons/{fixture['lesson_id']}/live"
            )
            await asyncio.sleep(1)

    try:
        for index, api in enumerate([teacher, *students]):
            cookie = "; ".join(f"{key}={value}" for key, value in api.cookies.items())
            socket = await connect(
                BASE_URL.replace("http", "ws", 1)
                + "/api/v1/ws?role="
                + ("teacher" if index == 0 else "student"),
                origin=BASE_URL,
                additional_headers={"Cookie": cookie},
                **({"ssl": tls} if BASE_URL.startswith("https:") else {}),
            )
            sockets.append(socket)
            tasks.append(asyncio.create_task(listen(socket, index)))
        await request(teacher, "POST", f"/teacher/lessons/{fixture['lesson_id']}/start")
        console = asyncio.create_task(live_console())

        async def probe_ui():
            async def open_card(index, page):
                queue = page.get_by_role("navigation", name="Активные", exact=True)
                await queue.get_by_role("button").first.wait_for(timeout=30000)
                start = time.perf_counter()
                await queue.get_by_role("button").first.click()
                await page.get_by_test_id("processing-timer").wait_for(timeout=10000)
                await page.evaluate(
                    "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
                ui_timings.append(round((time.perf_counter() - start) * 1000, 2))
                opened_in_browser[index].set()

            await asyncio.gather(
                *(open_card(index, page) for index, page in enumerate(pages))
            )

        probe = asyncio.create_task(probe_ui())

        async def act(index, api, student):
            # Deterministic staggering represents independent users, not a synchronized robot.
            await asyncio.sleep(index * args.think_time / users)
            for number, _ in enumerate(student["assignments"]):
                assignment = await asyncio.wait_for(queues[index].get(), timeout=120)
                assert assignment in student["assignments"]
                # The first five real browser users open their own card. Do not
                # concurrently open that same row a second time from their API driver.
                if number == 0 and index < len(opened_in_browser):
                    await asyncio.wait_for(opened_in_browser[index].wait(), timeout=30)
                card = await request(api, "GET", f"/student/assignments/{assignment}")
                assert "reference" not in card.json()
                for phase, status in enumerate(
                    (
                        "ACCEPTED",
                        "RESPONSE_STARTED",
                        "ARRIVED",
                        "WORK_IN_PROGRESS",
                        "WORK_COMPLETED",
                    )
                ):
                    # Reproducible independent action intervals (75–125% of the
                    # stated mean), avoiding an artificial synchronized class.
                    delay = args.think_time * (
                        0.75 + ((index * 37 + phase * 13 + number * 7) % 101) / 200
                    )
                    await asyncio.sleep(delay)
                    await request(
                        api,
                        "POST",
                        f"/student/assignments/{assignment}/status",
                        json={
                            "status": status,
                            "comment": "Пожар по адресу: ул. Дубнинская, д. 28. Пострадавших нет."
                            if status == "ACCEPTED"
                            else None,
                        },
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                await request(api, "GET", "/student/state")

        acting_tasks = [
            asyncio.create_task(act(i, api, row))
            for i, (api, row) in enumerate(zip(students, fixture["students"]))
        ]
        await asyncio.gather(*acting_tasks)
        await probe
        running = False
        await console
        deadline = time.monotonic() + 5
        while (
            sum(event["type"] == "STUDENT_ACTION" for event in received[0])
            < card_count * 7
        ):
            assert time.monotonic() < deadline, "Missing teacher events"
            await asyncio.sleep(0.02)
        events = [event for event in received[0] if event["type"] == "STUDENT_ACTION"]
        assert len(events) == card_count * 7, len(events)
        counts = Counter(event["payload"]["assignment_id"] for event in events)
        assert len(counts) == card_count and set(counts.values()) == {7}
        for index, row in enumerate(fixture["students"], 1):
            delivered = [
                event["payload"]["assignment_id"]
                for event in received[index]
                if event["type"] == "CARD_DELIVERED"
            ]
            assert Counter(delivered) == Counter(row["assignments"])
            assert sum(
                event["type"] == "CARD_CLOSED" for event in received[index]
            ) == len(row["assignments"])
            for event in received[index]:
                payload = event["payload"]
                if "student_id" in payload:
                    assert payload["student_id"] == row["id"]
        report_start = time.perf_counter()
        report = (
            await request(
                teacher, "GET", f"/teacher/lessons/{fixture['lesson_id']}/report"
            )
        ).json()
        report_seconds = time.perf_counter() - report_start
        graded = [card for student in report["students"] for card in student["cards"]]
        assert len(graded) == card_count and all(
            card["score"] and card["state"] == "CLOSED" for card in graded
        )
        assert all(card["score"]["grammar"]["available"] for card in graded)
        result = {
            "passed": p95(timings) < args.max_p95_ms
            and max(action_latency) < 2
            and report_seconds < 30
            and (not ui_timings or max(ui_timings) <= 2000),
            "sessions": users,
            "cards": card_count,
            "requests": len(timings),
            "p95_ms": p95(timings),
            "teacher_updates": len(events),
            "lost_ws_events": 0,
            "max_ws_seconds": round(max(action_latency), 3),
            "report_seconds": round(report_seconds, 3),
            "ui_ms": ui_timings,
            "grammar_enabled": True,
            "grammar_checked_cards": len(graded),
            "think_time_seconds": args.think_time,
            "think_time_range": [args.think_time * 0.75, args.think_time * 1.25],
            "max_ws_by_kind": {
                kind: round(max(values), 3) for kind, values in latency_by_kind.items()
            },
            "by_operation": {
                key: {"count": len(values), "p95_ms": p95(values)}
                for key, values in by_route.items()
                if len(values) > 1
            },
        }
        args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
        assert result["p95_ms"] < args.max_p95_ms, result
        assert result["max_ws_seconds"] < 2, result
        assert report_seconds < 30, result
        assert not ui_timings or max(ui_timings) <= 2000, result
    finally:
        running = False
        for task in acting_tasks:
            task.cancel()
        await asyncio.gather(*acting_tasks, return_exceptions=True)
        for attempt in range(10):
            try:
                finished = await teacher.post(
                    f"/teacher/lessons/{fixture['lesson_id']}/finish"
                )
                finished.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 9:
                    raise
                await asyncio.sleep(1)
        for socket in sockets:
            await socket.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(api.aclose() for api in [teacher, *students]))
        if browser:
            await browser.close()
            await playwright.stop()
        cleanup = await asyncio.to_thread(
            subprocess.run,
            compose.command(
                ROOT,
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "backend",
                    "python",
                    "-m",
                    "tests.cleanup_load_acceptance",
                ],
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        print(cleanup.stdout.strip())


asyncio.run(main())
