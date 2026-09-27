"""Stop one of two backend containers during an isolated lesson, then rejoin it."""

import asyncio
import json
import os
import ssl
import subprocess
import time
from contextlib import ExitStack, closing
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from browser_environment import BASE_URL, ROOT
from check_recovery_persistence import api, login
from technical_operations import containers, project_name


async def main():
    candidates = [
        row
        for row in containers(project_name(ROOT))
        if row["Config"]["Labels"]["com.docker.compose.service"] == "backend"
        and row["State"]["Running"]
    ]
    assert len(candidates) == 2, "Prepare exactly two backends before this acceptance"
    target = candidates[0]["Id"]
    output = ROOT / "artifacts/ui-review/full-tz-failover.json"
    result = {"passed": False, "container": target[:12], "replicas": 2}
    sockets, readers, disconnected, received = [], [], set(), []
    stopped, lesson_id = False, None
    tls = ssl.create_default_context(cafile=ROOT / ".secrets/pki/ca.crt")
    accounts = (
        json.loads((ROOT / "data/browser-acceptance.json").read_text(encoding="utf-8"))
        if os.environ.get("DISPATCHER_BROWSER_USERS")
        else {}
    )

    async def listen(connection, index):
        try:
            async for raw in connection:
                received.append((index, json.loads(raw)))
        except ConnectionClosed:
            disconnected.add(index)

    with ExitStack() as stack:
        teacher = stack.enter_context(
            closing(
                login(BASE_URL, ROOT, accounts.get("teacher", "teacher"), "teacher")
            )
        )
        student = stack.enter_context(
            closing(
                login(BASE_URL, ROOT, accounts.get("student1", "student1"), "student")
            )
        )
        assert api(student, "/student/state")["lesson"] is None
        identity = api(student, "/auth/me")["id"]
        scenario = next(
            row
            for row in api(teacher, "/teacher/scenarios?status=APPROVED&limit=200")[
                "items"
            ]
            if row["title"] == "Учебный пожар: двор"
        )
        try:
            lesson = api(
                teacher,
                "/teacher/lessons",
                {
                    "title": "Приёмка отказа backend " + str(uuid4())[:8],
                    "participants": [{"student_id": identity}],
                    "settings": {
                        "primary_status_deadline_sec": 3600,
                        "card_processing_deadline_sec": 3600,
                    },
                },
            )
            lesson_id = lesson["id"]
            assigned = api(
                teacher,
                f"/teacher/lessons/{lesson_id}/assign",
                {
                    "assignments": [
                        {"student_id": identity, "scenario_id": scenario["id"]}
                    ]
                },
            )
            assignment = assigned["assignment_ids"][0]
            cookie = "; ".join(
                f"{key}={value}" for key, value in student.cookies.items()
            )
            for index in range(10):
                connection = await connect(
                    BASE_URL.replace("http", "ws", 1) + "/api/v1/ws?role=student",
                    origin=BASE_URL,
                    additional_headers={"Cookie": cookie},
                    ssl=tls,
                    close_timeout=3,
                )
                heartbeat = json.loads(await asyncio.wait_for(connection.recv(), 10))
                assert heartbeat["type"] == "HEARTBEAT"
                sockets.append(connection)
                readers.append(asyncio.create_task(listen(connection, index)))
            api(teacher, f"/teacher/lessons/{lesson_id}/start", {})
            for _ in range(100):
                if api(student, "/student/state")["cards"]:
                    break
                await asyncio.sleep(0.1)
            path = f"/student/assignments/{assignment}"
            before = api(student, path)
            key = str(uuid4())
            body = {
                "status": "ACCEPTED",
                "comment": "Пожар по адресу: Дубнинская улица, дом 28. Пострадавших нет.",
            }
            accepted = student.post(
                path + "/status", json=body, headers={"Idempotency-Key": key}
            )
            accepted.raise_for_status()
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "stop", "--time", "1", target],
                check=True,
                capture_output=True,
                timeout=30,
            )
            stopped = True
            start = time.perf_counter()
            repeat = student.post(
                path + "/status", json=body, headers={"Idempotency-Key": key}
            )
            repeat.raise_for_status()
            assert repeat.json() == accepted.json()
            result["request_after_loss_ms"] = round(
                (time.perf_counter() - start) * 1000, 2
            )
            after = api(student, path)
            assert all(
                before["timers"][field] == after["timers"][field]
                for field in ("primary_deadline_at", "processing_deadline_at")
            )
            assert (
                sum(row["status"] == "ACCEPTED" for row in after["my_block"]["history"])
                == 1
            )
            for _ in range(100):
                if disconnected:
                    break
                await asyncio.sleep(0.1)
            assert 0 < len(disconnected) < len(sockets), disconnected
            live = api(teacher, f"/teacher/lessons/{lesson_id}/live")
            assert next(
                row for row in live["students"] if row["student_id"] == identity
            )["online"]
            for status in (
                "RESPONSE_STARTED",
                "ARRIVED",
                "WORK_IN_PROGRESS",
                "WORK_COMPLETED",
            ):
                response = student.post(
                    path + "/status",
                    json={"status": status},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                response.raise_for_status()
            for _ in range(100):
                if any(
                    event["type"] == "CARD_CLOSED"
                    for index, event in received
                    if index not in disconnected
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(
                    "Surviving backend did not deliver the closing event"
                )
            result.update(
                passed=True,
                disconnected_sockets=len(disconnected),
                surviving_sockets=len(sockets) - len(disconnected),
                duplicate_status_events=0,
                deadlines_unchanged=True,
            )
        finally:
            if stopped:
                await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "start", target],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            if lesson_id:
                api(teacher, f"/teacher/lessons/{lesson_id}/finish", {})
            await asyncio.gather(*(connection.close() for connection in sockets))
            await asyncio.gather(*readers, return_exceptions=True)
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
