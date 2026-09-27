"""Teacher console: observe, correct, assign, receive an action, finish."""
import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

fixture = json.loads(Path('data/live-acceptance.json').read_text(encoding='utf-8'))
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    teacher_context = browser.new_context(viewport={'width':1440,'height':1100})
    student_context = browser.new_context(viewport={'width':1440,'height':1100})
    teacher = teacher_context.new_page()
    student = student_context.new_page()
    errors = []
    for page, name, password in ((teacher,fixture['teacher'],'teacher'),(student,fixture['students'][0]['login'],'student')):
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://localhost:5173')
        page.get_by_label('Логин',exact=True).fill(name)
        page.get_by_label('Пароль',exact=True).fill(password)
        page.get_by_role('button',name='Вход в учебную систему',exact=True).click()
    teacher.get_by_role('button',name='Открыть пульт',exact=True).first.click()
    table = teacher.get_by_role('table',name='Участники занятия',exact=True)
    expect(table.get_by_role('row')).to_have_count(21)
    first = table.get_by_role('row').filter(has_text='АРМ-01')
    first.get_by_role('button').click()
    expect(teacher.get_by_role('heading',name='Корректировка оценки',exact=True)).to_be_visible()
    teacher.get_by_label('Новая оценка',exact=True).fill('82.5')
    teacher.get_by_label('Обоснование корректировки',exact=True).fill('Преподаватель учёл допустимое решение в условиях карточки.')
    teacher.get_by_role('button',name='Сохранить корректировку',exact=True).click()
    expect(teacher.get_by_text('Корректировка сохранена в журнале аудита.',exact=True)).to_be_visible()
    result = teacher.evaluate("async id => await (await fetch('/api/v1/teacher/assignments/'+id)).json()",fixture['students'][0]['assignment_id'])
    assert result['score']['total'] != 82.5 and result['effective_score']['total'] == 82.5
    teacher.get_by_label('Получатель задания',exact=True).select_option(fixture['students'][0]['id'])
    teacher.get_by_label('Сценарий задания',exact=True).select_option(label='Учебный пожар: двор')
    teacher.get_by_role('button',name='Отправить задание',exact=True).click()
    expect(teacher.get_by_text('Задание поставлено в очередь выдачи.',exact=True)).to_be_visible()
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        state=student.evaluate("async () => await (await fetch('/api/v1/student/state')).json()")
        active=[row for row in state['cards'] if row['state']=='DELIVERED']
        if active: break
        student.wait_for_timeout(100)
    assert active
    card=active[0]
    teacher.get_by_role('navigation',name='Карточки обучающегося',exact=True).get_by_role('button').filter(has_text=card['card_number']).click()
    observed=teacher.evaluate("async id => await (await fetch('/api/v1/teacher/assignments/'+id)).json()",card['assignment_id'])
    assert observed['timers']['processing_deadline_at'] is None, observed
    student.get_by_role('navigation',name='Активные',exact=True).get_by_role('button').filter(has_text=card['card_number']).click()
    student.get_by_role('button',name='Принята',exact=True).click()
    started=time.monotonic()
    student.get_by_role('button',name='Сохранить статус',exact=True).click()
    expect(first.get_by_role('cell').last).to_contain_text('Принята',timeout=2000)
    latency=time.monotonic()-started
    assert latency<2,latency
    teacher.screenshot(path='artifacts/stage8-console.png',full_page=True)
    teacher_context.set_offline(True)
    expect(teacher.get_by_text('Соединение потеряно',exact=False)).to_be_visible(timeout=18000)
    teacher_context.set_offline(False)
    expect(teacher.get_by_text('● Соединение с сервером установлено',exact=True)).to_be_visible(timeout=15000)
    teacher.get_by_role('button',name='Завершить занятие',exact=True).last.click()
    expect(student.get_by_role('heading',name='Результаты занятия',exact=True)).to_be_visible(timeout=10000)
    expect(teacher.get_by_role('heading',name='Корректировка оценки',exact=True)).to_be_visible()
    teacher.screenshot(path='artifacts/stage8-correction.png',full_page=True)
    assert not errors,errors
    browser.close()
Path('artifacts/stage8-browser.json').write_text(json.dumps({'passed':True,'action_latency_sec':round(latency,3)},indent=2),encoding='utf-8')
print('PASS: observation, original/corrected scores, manual assignment, live action, reconnect, finish')
