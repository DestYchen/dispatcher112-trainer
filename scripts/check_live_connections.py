"""Twenty real HTTP/WebSocket student sessions and one teacher console."""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

fixture = json.loads(Path('data/live-acceptance.json').read_text(encoding='utf-8'))

async def login(name, password):
    api = httpx.AsyncClient(base_url='http://localhost:8000/api/v1', timeout=20)
    csrf = (await api.get('/auth/me')).json()['error']['details']['csrf_token']
    response = await api.post('/auth/login', headers={'X-CSRF-Token':csrf}, json={'login':name,'password':password})
    response.raise_for_status()
    api.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return api

async def main():
    teacher = await login(fixture['teacher'], 'teacher')
    students = await asyncio.gather(*(login(row['login'], 'student') for row in fixture['students']))
    received = [[] for _ in range(21)]
    tasks = []
    sockets = []
    latencies = []
    async def receive(socket, index):
        async for raw in socket:
            event = json.loads(raw)
            received[index].append(event)
            if index == 0 and event['type'] == 'STUDENT_ACTION':
                latencies.append((datetime.now(timezone.utc) - datetime.fromisoformat(event['ts'])).total_seconds())
    try:
        for index, api in enumerate([teacher, *students]):
            cookie = '; '.join(f'{key}={value}' for key, value in api.cookies.items())
            socket = await connect('ws://localhost:8000/api/v1/ws?role=' + ('teacher' if index == 0 else 'student'),
                                   origin='http://localhost:5173', additional_headers={'Cookie':cookie})
            sockets.append(socket)
            tasks.append(asyncio.create_task(receive(socket, index)))
        response = await teacher.post(f"/teacher/lessons/{fixture['lesson_id']}/start")
        response.raise_for_status()
        deadline = time.monotonic() + 10
        while not all(any(event['type'] == 'CARD_DELIVERED' for event in events) for events in received[1:]):
            assert time.monotonic() < deadline, 'Missing delivery'
            await asyncio.sleep(.05)
        async def act(api, row):
            response = await api.get(f"/student/assignments/{row['assignment_id']}")
            response.raise_for_status()
            for status in ('ACCEPTED','RESPONSE_STARTED','ARRIVED','WORK_IN_PROGRESS','WORK_COMPLETED'):
                response = await api.post(f"/student/assignments/{row['assignment_id']}/status", json={'status':status}, headers={'Idempotency-Key':str(uuid4())})
                response.raise_for_status()
        await asyncio.gather(*(act(api, row) for api, row in zip(students, fixture['students'])))
        deadline = time.monotonic() + 5
        while sum(event['type'] == 'STUDENT_ACTION' for event in received[0]) < 140:
            assert time.monotonic() < deadline, 'Missing teacher update'
            await asyncio.sleep(.05)
        events = [event for event in received[0] if event['type'] == 'STUDENT_ACTION']
        assert len(events) == 140, len(events)
        for index, row in enumerate(fixture['students'], 1):
            own = [event['payload'] for event in events if event['payload']['student_id'] == row['id']]
            assert len(own) == 7
            assert [event['status'] for event in own if event['kind'] == 'STATUS_CHANGED'] == ['ACCEPTED','RESPONSE_STARTED','ARRIVED','WORK_IN_PROGRESS','WORK_COMPLETED']
            assert sum(event['type'] == 'CARD_CLOSED' for event in received[index]) == 1
        live = (await teacher.get(f"/teacher/lessons/{fixture['lesson_id']}/live")).json()
        assert len(live['students']) == 20 and all(row['online'] and row['closed'] == 1 for row in live['students'])
        result = {'sessions':20, 'teacher_updates':len(events), 'lost':0, 'max_action_latency_sec':round(max(latencies),3)}
        assert max(latencies) < 2, result
        Path('artifacts/stage8-connections.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(result)
    finally:
        for socket in sockets:
            await socket.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(api.aclose() for api in [teacher,*students]))

asyncio.run(main())
