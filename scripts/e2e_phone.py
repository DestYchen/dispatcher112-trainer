"""Real audio, keyboard dialling, offline report replay and its score."""
import json
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

with httpx.Client(base_url='http://localhost:8000/api/v1', timeout=15) as api:
    csrf=api.get('/auth/me').json()['error']['details']['csrf_token']
    response=api.post('/auth/login',headers={'X-CSRF-Token':csrf},json={'login':'teacher','password':'teacher'})
    response.raise_for_status()
    api.headers['X-CSRF-Token']=response.json()['csrf_token']
    students=api.get('/teacher/participants').json()['students']
    student_id=next(row['id'] for row in students if row['name']=='Иванов Иван')
    scenario=next(row for row in api.get('/teacher/scenarios').json()['items'] if row['title']=='Учебный пожар: доклад дежурному')
    result=api.post('/teacher/lessons',json={'title':'Проверка телефонии '+str(int(time.time())), 'participants':[{'student_id':student_id}], 'settings':{'grammar_check_enabled':False}})
    result.raise_for_status()
    lesson_id=result.json()['id']
    result=api.post(f'/teacher/lessons/{lesson_id}/assign',json={'assignments':[{'student_id':student_id,'scenario_id':scenario['id']}]})
    result.raise_for_status()
    assignment_id=result.json()['assignment_ids'][0]
    api.post(f'/teacher/lessons/{lesson_id}/start').raise_for_status()
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1100})
            context.add_init_script("""window.audioStarts=[]; const play=HTMLMediaElement.prototype.play;
            HTMLMediaElement.prototype.play=function(){ const url=this.src; this.addEventListener('playing',()=>window.audioStarts.push({url,at:performance.now()}),{once:true}); return play.call(this); };""")
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto('http://localhost:5173')
            page.get_by_label('Логин',exact=True).fill('student1')
            page.get_by_label('Пароль',exact=True).fill('student')
            page.get_by_role('button',name='Вход в учебную систему',exact=True).click()
            page.get_by_role('navigation',name='Активные',exact=True).get_by_role('button').first.click()
            expect(page.get_by_role('button',name='Телефония · Alt+T',exact=True)).to_be_enabled()
            page.keyboard.press('Alt+T')
            number=page.get_by_label('Номер абонента',exact=True)
            expect(number).to_be_focused()
            number.fill('999')
            page.get_by_role('button',name='Вызов',exact=True).click()
            expect(page.get_by_text('Номер 999 не отвечает. Проверьте номер по справочнику.',exact=True)).to_be_visible()
            number.fill('2201')
            page.evaluate('window.callStarted=performance.now()')
            page.get_by_role('button',name='Вызов',exact=True).click()
            page.wait_for_function("window.audioStarts.some(item=>item.url.endsWith('male_calm/greeting.wav'))")
            latency=page.evaluate("window.audioStarts.find(item=>item.url.endsWith('male_calm/greeting.wav')).at-window.callStarted")
            assert latency<500,latency
            report=page.get_by_role('textbox',name='Ваш доклад',exact=True)
            expect(report).to_be_focused()
            report.fill('Пожар, ул. Дубнинская, д. 28. Пострадавших нет. Бригада направлена.')
            page.keyboard.press('Escape')
            expect(page.get_by_role('button',name='Телефония · Alt+T',exact=True)).to_be_focused()
            page.keyboard.press('Alt+T')
            expect(report).to_have_value('Пожар, ул. Дубнинская, д. 28. Пострадавших нет. Бригада направлена.')
            context.set_offline(True)
            played_before=page.evaluate('window.audioStarts.length')
            page.get_by_role('button',name='Повторить реплику',exact=True).click()
            page.wait_for_function('count => window.audioStarts.length > count',arg=played_before)
            page.get_by_role('button',name='Завершить доклад',exact=True).click()
            page.wait_for_function("Object.entries(localStorage).some(([key,value])=>key.startsWith('dispatcher112-actions:')&&JSON.parse(value).some(action=>action.kind==='report'))")
            context.set_offline(False)
            expect(page.get_by_text('Я вас понял, информация принята.',exact=True)).to_be_visible(timeout=15000)
            page.screenshot(path='artifacts/stage9-phone.png',full_page=True)
            page.keyboard.press('Escape')
            for status in ('Принята','Реагирование начато','Прибыли','Работы ведутся','Работы завершены 🔒'):
                page.get_by_role('button',name=status,exact=True).click()
                page.get_by_role('button',name='Сохранить статус',exact=True).click()
                if status.endswith('🔒'):
                    page.get_by_role('dialog').get_by_role('button',name='Закрыть карточку',exact=True).click()
                else:
                    expect(page.get_by_role('button',name=status,exact=True)).not_to_be_visible()
            expect(page.get_by_text('Карточка закрыта',exact=True).first).to_be_visible()
            assert not errors,errors
            browser.close()
        result=api.get(f'/teacher/assignments/{assignment_id}').json()
        assert result['score']['axes']['completeness']['score']==100,result['score']
        reports=[event for event in result['events'] if event['kind']=='PHONE_REPORT']
        assert len(reports)==1
        Path('artifacts/stage9-browser.json').write_text(json.dumps({'greeting_latency_ms':round(latency,1),'reports':len(reports),'completeness':100},indent=2),encoding='utf-8')
        print('PASS: audio, unknown number, keyboard, draft, offline report, score',round(latency,1),'ms')
    finally:
        api.post(f'/teacher/lessons/{lesson_id}/finish').raise_for_status()
