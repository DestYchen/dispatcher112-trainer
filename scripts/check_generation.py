"""Exercise the real arq worker and persist measured generation progress."""
import json
import time
from pathlib import Path

import httpx

with httpx.Client(base_url='http://localhost:8000/api/v1', timeout=10) as api:
    pre = api.get('/auth/me').json()['error']['details']['csrf_token']
    login = api.post('/auth/login', json={'login':'teacher','password':'teacher'}, headers={'X-CSRF-Token':pre})
    login.raise_for_status()
    api.headers['X-CSRF-Token'] = login.json()['csrf_token']
    student = api.get('/teacher/participants').json()['students'][0]['id']
    response = api.post('/teacher/lessons', json={'title':'Проверка генерации 40 сценариев','participants':[{'student_id':student}]})
    response.raise_for_status()
    lesson_id = response.json()['id']
    result = api.post(f'/teacher/lessons/{lesson_id}/scenarios/generate', json={'count':40})
    result.raise_for_status()
    job_id = result.json()['job_id']
    started = time.monotonic()
    while time.monotonic() - started < 90:
        progress = api.get(f'/teacher/jobs/{job_id}').json()
        if progress['status'] in ('DONE','FAILED'):
            break
        time.sleep(1)
    assert progress['status'] == 'DONE', progress
    assert progress['generated'] + progress['rejected'] == 40
    assert progress['rejected'] < 10
    progress.update(lesson_id=lesson_id, seconds=round(time.monotonic()-started,3))
    Path('artifacts/stage7-generation.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(progress,ensure_ascii=False))
