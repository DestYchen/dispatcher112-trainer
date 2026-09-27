# API.md — Контракты

База: `/api/v1`. Формат — JSON, UTF-8. Все временные метки — ISO-8601 с таймзоной (`2026-09-17T14:22:05+03:00`).

---

## 0. Общие соглашения

### 0.1. Ответ об ошибке

**MUST** Единый формат для всех ошибок:

```json
{
  "error": {
    "code": "INVALID_TRANSITION",
    "message": "Переход из «Работы завершены» невозможен: карточка закрыта.",
    "details": { "from": "WORK_COMPLETED", "to": "ARRIVED" },
    "request_id": "01JBX7K2N4..."
  }
}
```

`message` **MUST** быть на русском и пригоден для показа пользователю. `code` — машиночитаемый.

### 0.2. Коды ошибок

| HTTP | `code` | Когда |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` | невалидное тело запроса |
| 401 | `UNAUTHENTICATED` | нет или истекла сессия |
| 403 | `FORBIDDEN` | роль не позволяет |
| 404 | `NOT_FOUND` | объект не существует или недоступен |
| 409 | `INVALID_TRANSITION` | недопустимый переход статуса |
| 409 | `CARD_CLOSED` | карточка закрыта для редактирования |
| 409 | `LESSON_NOT_RUNNING` | занятие не идёт |
| 422 | `COMMENT_REQUIRED` | обязательный комментарий отсутствует или короче 15 символов |
| 429 | `RATE_LIMITED` | превышен лимит попыток |
| 500 | `INTERNAL_ERROR` | непредвиденная ошибка |

### 0.3. Аутентификация

JWT в httpOnly-cookie `session`. Все эндпойнты кроме `/auth/*` требуют сессии.

Мутирующие запросы **MUST** передавать `X-CSRF-Token` (значение выдаётся в `/auth/me`).

### 0.4. Идемпотентность

**MUST** `POST /student/assignments/{id}/status` и `POST /student/assignments/{id}/report` принимают заголовок `Idempotency-Key`. Повторный запрос с тем же ключом в течение 10 минут возвращает первый результат, не создавая дубликат.

### 0.5. Пагинация

`?limit=50&cursor=<opaque>`. Ответ: `{ "items": [...], "next_cursor": "..." | null }`.

---

## 1. Аутентификация

### `POST /auth/login`

```json
{ "login": "ivanov", "password": "...", "totp": "123456" }
```

`totp` обязателен, если у пользователя задан `totp_secret`.

**200**

```json
{
  "user": {
    "id": "uuid",
    "login": "ivanov",
    "full_name": "Иванов Пётр Сергеевич",
    "short_name": "Иванов П. С.",
    "role": "STUDENT",
    "service": { "code": "DDS_CHERTANOVO", "name": "ДДС Чертаново Южное" }
  },
  "csrf_token": "..."
}
```

**401** `UNAUTHENTICATED` — «Неверный логин или пароль.»
**429** `RATE_LIMITED` — «Слишком много попыток. Повторите через 15 минут.»

### `POST /auth/logout` → **204**

### `GET /auth/me` → как `user` из login + `csrf_token` + `server_time`

---

## 2. Обучающийся

### `GET /student/state`

Полное состояние рабочего места. **MUST** вызываться при загрузке и после реконнекта WebSocket.

**200**

```json
{
  "server_time": "2026-09-17T14:22:05+03:00",
  "lesson": {
    "id": "uuid",
    "title": "Аварии в городском хозяйстве",
    "status": "RUNNING",
    "settings": {
      "primary_status_deadline_sec": 30,
      "card_processing_deadline_sec": 180,
      "hints_enabled": false,
      "grammar_check_enabled": true
    }
  },
  "workstation": { "number": "АРМ-07" },
  "cards": [ /* CardSummary[] */ ],
  "stats": { "closed": 4, "expired": 1, "avg_primary_delay_ms": 21400 }
}
```

Если активного занятия нет — `lesson: null`, `cards: []`.

### Объект `CardSummary`

```json
{
  "assignment_id": "uuid",
  "card_number": "2026-0917-004412",
  "state": "DELIVERED",
  "origin": "OPERATOR_112",
  "incident_type_name": "пожар: частный дом",
  "address_short": "ул. Станционная, д. 28",
  "registered_at": "2026-09-17T14:21:40+03:00",
  "delivered_at": "2026-09-17T14:22:00+03:00",
  "primary_deadline_at": "2026-09-17T14:22:30+03:00",
  "processing_deadline_at": null,
  "current_status": null,
  "is_overdue": false,
  "has_unread_description": true
}
```

`processing_deadline_at` появляется после открытия карточки.

### `GET /student/assignments/{id}`

Полная карточка.

**200**

```json
{
  "assignment_id": "uuid",
  "state": "OPENED",
  "card": {
    "card_number": "2026-0917-004412",
    "origin": "OPERATOR_112",
    "registered_at": "2026-09-17T14:21:40+03:00",
    "operator_workstation": "ОП-034",
    "applicant": { "name": "Иванова И. С.", "phone": "+7 916 ***-**-71" },
    "address": {
      "raw": "ул. Станционная, д. 28",
      "clarification": "при уточнении адреса — г. Королёв, МО",
      "lat": 55.9142,
      "lon": 37.8258
    },
    "attributes": ["на улице", "частный дом", "открытое пламя"],
    "incident_type_name": "пожар: частный дом",
    "modifiers": [
      { "code": "THREAT_TO_PEOPLE", "label": "Угроза людям" }
    ],
    "description": "Горит крыша частного дома, пострадавших нет, дом не газифицирован.",
    "notification_list": [
      {
        "service_code": "MCHS",
        "service_name": "МЧС",
        "is_own": false,
        "last_status": { "status": "ACCEPTED", "at": "2026-09-17T14:22:11+03:00", "by": "оп. 9999" }
      },
      {
        "service_code": "DDS_CHERTANOVO",
        "service_name": "ДДС Чертаново Южное",
        "is_own": true,
        "last_status": null
      }
    ]
  },
  "my_block": {
    "service_code": "DDS_CHERTANOVO",
    "current_status": null,
    "available_statuses": [
      { "code": "ACCEPTED", "label": "Принята", "comment_required": false },
      { "code": "NOT_ACCEPTED", "label": "Не принята", "comment_required": true }
    ],
    "history": []
  },
  "timers": {
    "server_time": "2026-09-17T14:22:08+03:00",
    "primary_deadline_at": "2026-09-17T14:22:30+03:00",
    "processing_deadline_at": "2026-09-17T14:25:08+03:00"
  }
}
```

**MUST** `available_statuses` вычисляется сервером по автомату. Фронтенд рендерит ровно то, что пришло.

**MUST** Первый успешный вызов этого эндпойнта переводит `state` в `OPENED`, ставит `opened_at` и создаёт `status_events` с `RECEIVED`.

### `POST /student/assignments/{id}/status`

```json
{
  "status": "NOT_ACCEPTED",
  "comment": "Дом обслуживает УК «ПИК», информация передана в их диспетчерскую."
}
```

**200** — обновлённый `my_block` + `timers`.

**409** `INVALID_TRANSITION`, `CARD_CLOSED`
**422** `COMMENT_REQUIRED` — «К статусу «Не принята» нужен комментарий: укажите причину отказа и куда передана информация.»

### `POST /student/assignments/{id}/check-text`

Проверка текста до отправки. Вызывается с debounce 600 мс.

```json
{ "text": "Передано в диспетчерскую на Дубининской улице", "field": "comment" }
```

**200**

```json
{
  "issues": [
    {
      "kind": "ADDRESS_TYPO",
      "severity": "CRITICAL",
      "offset": 31,
      "length": 12,
      "text": "Дубининской",
      "message": "Такой улицы нет в справочнике.",
      "suggestions": ["Дубнинская улица", "Дубининская улица"]
    },
    {
      "kind": "SPELLING",
      "severity": "MINOR",
      "offset": 9,
      "length": 3,
      "text": "диспетчерскую",
      "message": "Возможная опечатка.",
      "suggestions": ["диспетчерскую"]
    }
  ]
}
```

**MUST** Эндпойнт не сохраняет ничего и не влияет на оценку. Оценка пересчитывается независимо при закрытии карточки.

### `GET /student/directory`

Телефонный справочник.

**200**

```json
{
  "entries": [
    { "code": "DUTY_OFFICER", "number": "2201", "title": "Оперативный дежурный" },
    { "code": "HEAD_ENGINEER", "number": "2214", "title": "Главный инженер" },
    { "code": "SYSTEM_112",   "number": "112",  "title": "Служба 112" }
  ]
}
```

### `POST /student/assignments/{id}/call`

Начало вызова.

```json
{ "number": "2201" }
```

**200**

```json
{
  "call_id": "uuid",
  "callee": { "code": "DUTY_OFFICER", "title": "Оперативный дежурный" },
  "greeting_audio_url": "/media/voices/male_calm/greeting.wav",
  "greeting_text": "Слушаю вас."
}
```

**404** — «Номер 2201 не отвечает. Проверьте номер по справочнику.»

### `POST /student/assignments/{id}/report`

Завершение доклада.

```json
{
  "call_id": "uuid",
  "transcript": "Диспетчер Иванов, ДДС Чертаново Южное. Пожар, ул. Станционная, 28. Пострадавших нет. Бригада направлена.",
  "duration_ms": 18400
}
```

**200**

```json
{
  "confirmation_audio_url": "/media/voices/male_calm/confirm.wav",
  "confirmation_text": "Я вас понял, информация принята."
}
```

### `GET /student/results?lesson_id=...`

Свои результаты после занятия.

**200**

```json
{
  "lesson": { "id": "uuid", "title": "...", "finished_at": "..." },
  "summary": {
    "total": 76.4,
    "cards_total": 8,
    "cards_closed": 7,
    "cards_expired": 1,
    "axes": { "timeliness": 82.0, "correctness": 71.0, "completeness": 80.0, "literacy": 90.0 }
  },
  "cards": [
    {
      "card_number": "2026-0917-004412",
      "incident_type_name": "пожар: частный дом",
      "total": 68.0,
      "violations": [
        {
          "code": "INCOMPLETE_COMMENT",
          "severity": "MAJOR",
          "message": "В комментарии не указано, куда передана информация.",
          "hint": "К статусу «Не принята» укажите причину отказа и службу."
        }
      ]
    }
  ]
}
```

---

## 3. Преподаватель

### `GET /teacher/lessons` → список занятий

### `POST /teacher/lessons`

```json
{
  "title": "Аварии в городском хозяйстве",
  "settings": { "...": "как в SPEC 3.4" },
  "participants": [
    { "student_id": "uuid", "workstation_id": "uuid" }
  ]
}
```

### `POST /teacher/lessons/{id}/scenarios/generate`

Запуск фоновой генерации.

```json
{
  "incident_group_ids": ["uuid", "uuid"],
  "difficulty_range": [1, 6],
  "count": 40,
  "origin_mix": { "OPERATOR_112": 0.8, "EXTERNAL_SYSTEM": 0.2 }
}
```

**202**

```json
{ "job_id": "uuid", "status": "QUEUED" }
```

### `GET /teacher/jobs/{job_id}`

```json
{ "job_id": "uuid", "status": "RUNNING", "progress": 0.45, "generated": 18, "rejected": 2 }
```

`status`: `QUEUED` | `RUNNING` | `DONE` | `FAILED`.

### `GET /teacher/scenarios?status=PENDING_REVIEW`

Список на проверку. Каждый элемент содержит `card_payload`, `reference` и результаты автовалидации.

### `POST /teacher/scenarios/{id}/approve`

```json
{ "difficulty": 4 }
```

### `POST /teacher/scenarios/{id}/reject`

```json
{ "comment": "Слишком простой вопрос, добавь противоречие в показаниях заявителя." }
```

**MUST** Комментарий сохраняется и используется при перегенерации. Возвращает новый `job_id`, если запрошена перегенерация.

### `POST /teacher/lessons/{id}/start` → **200**, занятие в `RUNNING`

### `POST /teacher/lessons/{id}/finish`

**200** — занятие в `FINISHED`, все открытые карточки закрываются, запускается пересчёт оценок.

### `POST /teacher/lessons/{id}/assign`

Ручная раздача заданий.

```json
{
  "assignments": [
    { "student_id": "uuid", "scenario_id": "uuid" },
    { "student_id": "uuid", "scenario_id": "uuid" }
  ]
}
```

### `GET /teacher/lessons/{id}/live`

Состояние пульта. Также транслируется по WebSocket.

**200**

```json
{
  "server_time": "2026-09-17T14:30:00+03:00",
  "lesson_status": "RUNNING",
  "elapsed_sec": 480,
  "students": [
    {
      "student_id": "uuid",
      "short_name": "Иванов П. С.",
      "workstation": "АРМ-07",
      "online": true,
      "active_cards": 2,
      "closed": 4,
      "expired": 1,
      "current_score": 76.4,
      "last_action": { "kind": "STATUS_SET", "status": "ACCEPTED", "at": "2026-09-17T14:29:51+03:00" },
      "alert": null
    }
  ],
  "aggregate": {
    "cards_delivered": 42,
    "cards_closed": 31,
    "cards_expired": 4,
    "avg_primary_delay_ms": 22100,
    "top_violations": [
      { "code": "MISSING_COMMENT", "count": 9 },
      { "code": "WRONG_PRIMARY_STATUS", "count": 6 }
    ]
  }
}
```

`alert` — `"OVERDUE"` | `"IDLE"` | `null`. `IDLE` ставится, если студент не совершал действий > 90 с при наличии активных карточек.

### `POST /teacher/assignments/{id}/override`

```json
{
  "axes": { "correctness": 85.0 },
  "total": 82.0,
  "comment": "Отказ обоснован, студент прав: объект не в зоне ответственности."
}
```

**MUST** Пишет `teacher_override`, не затирает `score`, логирует `SCORE_OVERRIDE`.

### `GET /teacher/lessons/{id}/report`

**200** — данные отчёта (см. UI.md, экран отчёта).

### `GET /teacher/lessons/{id}/report.pdf`

**200** `application/pdf`. Генерация через WeasyPrint из HTML-шаблона.

---

## 4. Администратор

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET/POST/PATCH` | `/admin/users` | CRUD пользователей |
| `POST` | `/admin/users/{id}/reset-password` | сброс пароля |
| `POST` | `/admin/users/{id}/block` | блокировка |
| `GET/POST` | `/admin/workstations` | рабочие места |
| `POST` | `/admin/classifier/import` | импорт xlsx, multipart |
| `GET` | `/admin/classifier/stats` | сводка: групп, типов, служб |
| `POST` | `/admin/streets/import` | импорт справочника улиц |
| `GET` | `/admin/audit?user_id=&from=&to=` | журнал аудита |
| `GET` | `/admin/health` | состояние компонентов |

`GET /admin/health`:

```json
{
  "database": { "ok": true, "latency_ms": 3 },
  "redis": { "ok": true },
  "languagetool": { "ok": true, "latency_ms": 41 },
  "generation_backend": { "kind": "template", "ok": true },
  "worker": { "ok": true, "queue_depth": 0 },
  "last_backup_at": "2026-09-17T03:00:00+03:00"
}
```

---

## 5. WebSocket

### 5.1. Подключение

`GET /api/v1/ws?role=student` — апгрейд соединения. Аутентификация по той же cookie.

**MUST** Реконнект с экспоненциальным backoff: 1 с, 2 с, 4 с, 8 с, далее 10 с. После восстановления клиент запрашивает `GET /student/state` или `/teacher/lessons/{id}/live` и заменяет состояние целиком.

### 5.2. Формат сообщения

```json
{ "type": "CARD_DELIVERED", "ts": "2026-09-17T14:22:00+03:00", "payload": { } }
```

### 5.3. События для студента

| `type` | `payload` | Действие в UI |
| --- | --- | --- |
| `LESSON_STARTED` | `{ lesson }` | переход с экрана ожидания на рабочий |
| `LESSON_FINISHED` | `{ lesson_id }` | блокировка ввода, переход к результатам |
| `CARD_DELIVERED` | `CardSummary` | карточка добавляется вверх списка, звук, счётчик 30 с |
| `CARD_EXPIRED` | `{ assignment_id }` | красная индикация строки |
| `CARD_CLOSED` | `{ assignment_id }` | строка уходит в «Закрытые» |
| `HEARTBEAT` | `{ server_time }` | синхронизация часов, раз в 5 с |

### 5.4. События для преподавателя

| `type` | `payload` |
| --- | --- |
| `STUDENT_JOINED` / `STUDENT_LEFT` | `{ student_id, workstation }` |
| `STUDENT_ACTION` | `{ student_id, assignment_id, kind, status }` |
| `STUDENT_ALERT` | `{ student_id, alert }` |
| `AGGREGATE_UPDATED` | `aggregate` |
| `JOB_PROGRESS` | `{ job_id, progress, generated, rejected }` |

**MUST** `HEARTBEAT` отправляется каждые 5 секунд обеим ролям. Клиент, не получивший его 15 секунд, считает соединение разорванным и переподключается.

## 6. Расширение: заполнение карточки по обращению

`lessons.settings.training_mode`: `CARD_ACTIONS` (по умолчанию), `CARD_ENTRY`, `MIXED`; прежний `scenario_mode` сохраняет смысл источника сценариев. Элемент назначения может содержать `task_mode: CARD_ACTIONS | CARD_ENTRY`. В однородном занятии несовпадение режима отклоняется с `400 VALIDATION_ERROR`.

У `CardSummary` и `CardDetail` добавлен `task_mode`. В режиме `CARD_ENTRY` очередь показывает «Входящее обращение», адрес эталона скрыт. Первое открытие означает приём сообщения, один раз устанавливает `opened_at` и `primary_status_at`; это событие аудируется. `available_statuses` пуст, ручная смена статуса отклоняется `409 INVALID_TRANSITION`. Источником обоих сроков остаются настройки занятия и серверное время.

`CardDetail.entry` содержит `incoming_message`, `draft`, `revision`, `accepted_delay_ms`, `submitted_at`, `score` (только после закрытия). `draft` имеет поля: `applicant: {name, phone}`, `address: {raw, clarification}`, `incident_type_id: UUID | null`, `description`, `modifiers: string[]`, `notified_services: string[]`. Ответ никогда не включает эталон до сдачи/закрытия. Текст обращения задаёт преподаватель либо он собирается из сведений сценария.

| Метод и путь (под `/api/v1`) | Контракт |
| --- | --- |
| `GET /student/entry-directory` | Группы, видимые службы и модификаторы. Чтение разрешено STUDENT/TEACHER для заполнения/подготовки |
| `GET /student/entry-types?group_id=...&q=...` | Типы группы, поиск по названию, до 200 вариантов за запрос |
| `GET /student/entry-types/{id}` | Выбранный тип, его группа и формализованные признаки |
| `GET /student/entry-services?incident_type_id=...&modifiers=...` | Детерминированная подборка служб; студент может изменить её вручную |
| `POST /student/assignments/{id}/draft` | `{revision, card}`; ответ `{draft, revision, server_time}`. Сохранение неполных полей разрешено |
| `POST /student/assignments/{id}/submit-card` | `{revision}`; закрывает сохранённый черновик, записывает неизменяемый снимок и оценку |
| `POST /teacher/assignments/{id}/reuse-card` | Создаёт один `PENDING_REVIEW` сценарий из сданной карточки своего занятия; повтор возвращает тот же сценарий |

Для `/draft` и `/submit-card` обязательны CSRF и `Idempotency-Key`; повтор с тем же ключом/телом возвращает исходный ответ. Несовпадение версии: `409 DRAFT_CONFLICT`; закрытая карточка: `409 CARD_CLOSED`; чужое или ещё не выданное задание: `404 NOT_FOUND`. Неверные UUID, службы, дубли и неизвестные поля отклоняются. Сдача с незаполненными полями разрешена и отражается в оценке, а не блокирует завершение упражнения.

`POST /teacher/scenarios/{id}/approve` дополнительно принимает `incoming_message` и `card` для исправления полей заготовки; `reference.entry_description_keywords` — обязательные слова/фразы описания, через список. Пустой список означает нормализованное текстовое сравнение. Старые запросы сохраняют прежний формат эталона. Автопроверка сценария выполняется перед утверждением.

Оценка `entry-1.0.0` сохраняет четыре оси и содержит `entry_fields` (ответ, эталон, совпадение и подпись), серверные интервалы и нарушения. JSON/PDF отчёт включает время заполнения, норматив и сравнение полей. Закрытие занятия преподавателем оценивает несданный черновик с `CARD_NOT_SUBMITTED` и нулевой полнотой, исходные введённые данные сохраняются.
## 7. Локальная SIP-телефония

`LessonSettings.incoming_channel`: `TEXT` по умолчанию или `VOICE`, применяется к заданиям `CARD_ENTRY`. В голосовом режиме GET карточки не принимает обращение; отметки первого приёма/заполнения устанавливает событие ответа Asterisk. Повторный звонок их не меняет. Старт голосового занятия требует заранее подготовленных WAV, иначе `409 VOICE_NOT_READY`.

Все маршруты ниже имеют префикс `/api/v1`. Авторизация и CSRF обязательны как в остальных API; секрет сессии никогда не кэшируется.

| Метод и путь | Доступ | Результат |
| --- | --- | --- |
| GET `/sip/session` | Студент/преподаватель | Собственные SIP URI, username/password, относительный WebSocket-путь, внутренний эхо-тест, активный вызов; `Cache-Control: no-store` |
| POST `/sip/assignments/{assignment_id}/calls` | Владелец карточки, активное занятие | `{direction: INBOUND/OUTBOUND, number?: string}`; `Idempotency-Key`. Входящий только для заполнения, исходящий только на номер учебного справочника после открытия |
| GET `/sip/assignments/{assignment_id}/calls` | Владелец карточки | До 100 последних вызовов |
| GET `/sip/calls/{call_id}` | Владелец вызова | Направление, состояние, серверные created_at/answered_at/ended_at, причина отказа, доступность записи |
| POST `/sip/calls/{call_id}/cancel` | Владелец вызова | Отмена непринятого звонка с аудитом; повтор идемпотентен. Принятый звонок завершается через SIP BYE |
| GET `/sip/calls/{call_id}/recording` | Студент или преподаватель его занятия | Приватный WAV завершённого вызова, поддерживается Range; чужой объект — 404 |

Состояния `sip_calls`: `REQUESTED`, `RINGING`, `CONNECTED`, `ENDED`, `FAILED`. У пользователя только один активный учебный вызов (`PHONE_BUSY`, 409). Наружных телефонных маршрутов нет. Неизвестный учебный номер возвращает 404. Доклад до подтверждённого ответа — `CALL_NOT_ANSWERED`, 409; его длительность рассчитывается по времени Asterisk, клиентское значение не используется. Непринятый запрос истекает через 60 с, разговор ограничен часом и завершается при закрытии карточки/занятия или блокировке пользователя.

В преподавательском GET `/teacher/assignments/{assignment_id}` добавлен массив `sip_calls`. Подготовка сценария и ответ студента сохраняют прежние идентификаторы и контракты. SIP-события сохраняются в аудите, начало/окончание разговора обновляют пульт.
## 8. Материалы, история, критерии и нейросетевой прогноз

Этап 14 расширяет контракты без переименования прежних сущностей. Все пути ниже начинаются с `/api/v1`; мутации требуют сессии и CSRF. Администратору учебные маршруты `/learning`, группы, материалы и аналитика недоступны. Пустые списки — `items: []`, чужой объект — `404`.

| Метод и путь | Доступ / содержание |
| --- | --- |
| GET /teacher/groups | Собственные группы и участники |
| POST /teacher/groups | `{title, student_ids}`; 1–100 различных действующих учащихся |
| PATCH /teacher/groups/{id} | Название/состав собственной группы; история не удаляется |
| GET /teacher/materials | Собственные материалы, включая архивные |
| POST /teacher/materials | multipart: title, body, difficulty 1–10, необязательный file PDF/DOCX ≤10 МиБ; нужен текст либо файл |
| PATCH /teacher/materials/{id} | `{archived}`; архив не включается в новые модули, прежние назначения сохраняются |
| GET /learning/materials/{id}/file | Автор либо ученик назначенного модуля; вложение, private/no-store; отсутствие файла — 503 MATERIAL_UNAVAILABLE |
| GET /teacher/modules | Собственные модули и назначенные группы |
| POST /teacher/modules | title, instructions, difficulty, material_ids, scenario_ids; собственные доступные материалы и утверждённые неархивные сценарии |
| POST /teacher/modules/{id}/groups/{group_id} | Назначение собственной группе, повтор без дубля |
| GET /student/modules | Только модули текущих групп ученика, тексты/ссылки материалов и отметки; эталоны сценариев не передаются |
| POST /student/modules/{id}/complete | Отметка изучения, повтор сохраняет прежнее серверное время |
| GET /teacher/progress/students | Участники своих занятий и групп |
| GET /learning/history | Ученик — собственная история; преподавателю нужен student_id, выдаются только его занятия; limit/cursor |
| POST /teacher/assignments/{id}/feedback | `{body}` 5–5000 символов, только своё оценённое задание; добавляет комментарий без изменения оценки |
| GET /teacher/analytics/models | Последние 100 собственных версий, метрики и контрольные сравнения |
| POST /teacher/analytics/models | dataset_kind SYNTHETIC/OBSERVED и dataset_note 15–2000 символов; обучение на своей истории между занятиями |
| POST /teacher/analytics/forecast | student_id, difficulty 1–10, task_mode; сохраняет прогноз последней моделью, повтор на тех же вводных возвращает его же |
| GET /learning/forecasts | Собственные прогнозы ученика либо прогнозы преподавателя для student_id; сравнение с первым подходящим будущим заданием |
| GET /teacher/groups/{id}/analytics | Средние исходные оценки, частые ошибки, средние последних прогнозов и количество охваченных учащихся |
| POST /teacher/scenarios/{id}/copy | Новая собственная версия PENDING_REVIEW, исходник сохраняется |
| PATCH /teacher/scenarios/{id}/archive | `{archived}` для собственного сценария; новые назначения запрещены |
| DELETE /teacher/scenarios/{id} | Только собственный неиспользованный сценарий; 409 SCENARIO_IN_USE при заданиях/модулях |
| GET /teacher/generation/locations | Локальный справочник улиц для подготовки сценариев |

Генерация принимает дополнительно `generation_backend: template | local_llm | null` и `street_ids: UUID[]`. null сохраняет прежнюю серверную настройку. Утверждение принимает необязательное `title`; поле `card` позволяет исправить все вводимые сведения. Список сценариев принимает `include_archived`, по умолчанию false, и возвращает `editable`/`archived`.

`LessonSettings.success_criteria` — null либо `{min_total, max_errors, max_spelling_errors, min_words, sentence_end_required, required_terms}`. Снимок результата `score.criteria` содержит rules, passed (true/false/null), errors и unavailable. null означает неполную проверку при недоступной грамотности. Обязательные нормативные комментарии и исходный балл не ослабляются.

ML: минимально 5 учащихся, 20 обучающих и 5 контрольных последовательностей после первых трёх заданий. Ошибки: `400 INSUFFICIENT_DATA`, `409 MODEL_UNAVAILABLE`, `409 LESSON_RUNNING`. Модель хранится в `learning_models`, прогноз — `learning_forecasts`; версии/данные хешируются, веса JSON, не pickle. Источники обучения — исходные регламентные оценки своих занятий. Модель не участвует в изменении оценки. Подробности и ограничения: `docs/LEARNING-AI.md`.
## 9. Техническое администрирование (этап 15, интеграция в работе)

Все маршруты только ADMIN; преподавательские API и `/ws?role=teacher` доступны только TEACHER. Техническое представление `/admin/audit` скрывает содержимое карточек, комментарии и оценки, сохраняя исходный неизменяемый аудит в БД.

- `GET /admin/policies`, `PATCH /admin/policies`: `session_minutes` 15–480, `login_attempts` 3–5, `lockout_minutes` 15–60, `min_password_length` 8–128, `require_admin_totp` boolean. Политика действует на HTTP/WebSocket и новые пароли; включение TOTP без настройки всех действующих администраторов — 409 `ADMIN_TOTP_REQUIRED`.
- `GET /admin/maintenance`, `POST /admin/maintenance`: `{enabled, reason}` (5–1000 символов). Активное занятие/генерация — 409 `LESSON_RUNNING`; параллельные изменения — 409 `OPERATION_IN_PROGRESS`. В режиме обслуживания изменяющие учебные/административные запросы временно дают 503 `MAINTENANCE`, кроме самого технического пульта и выхода из сессии.
- `GET /admin/operations/services`: состояние экземпляров, проверки здоровья, CPU, память, версия Docker, допустимые действия.
- `GET /admin/operations/services/{service}/logs`: последние 100 сообщений, известные секреты скрыты.
- `POST /admin/operations/jobs`: `{id: UUID, kind: service|resources|backup_create|backup_verify|backup_configure, service, action?: start|stop|restart, cpus?, memory_mb?, snapshot?, schedule?}`. Требует режима обслуживания. Основные сервисы допускают только restart. Ответ 202 с сохранённой задачей. Повтор id с другим телом — 409 `IDEMPOTENCY_CONFLICT`.
- `GET /admin/operations/jobs/{id}`: QUEUED/RUNNING/SUCCEEDED/FAILED; завершение фиксируется в аудите один раз. Фоновый монитор сервера записывает результат и освобождает текущую операцию даже после закрытия браузера. Если доставка исполнителю оборвалась до регистрации, повторяется тот же запрос с тем же id.
- `GET /admin/backups`: `{items, schedule, worker_ready, updated_at}`; в items только имя/дата/объём/число файлов, `schema`, `program_files`, целостность подписанного описания и последний сохранённый результат проверочного восстановления. `dispatcher-backup-2` содержит исходный код, старый `dispatcher-backup-1` имеет `program_files: 0`; повреждённое описание — null в этих полях. Создание и проверка также возвращают `schema` и `program_files` в результате операции. Содержимое файлов, конфигурация и ключи в API не возвращаются.
- `GET /admin/diagnostics/report`: агрегированное использование, технические сбои, текущие счётчики API и проверки БД/аудита. Параметры `from`/`to` — даты с часовым поясом, полуинтервал [from, to), 1 секунда–366 дней, по умолчанию последние 24 часа; `limit` 1–200 (50), `cursor`, `export` boolean. Ответ: `{generated_at, period: {from, to}, usage, inventory, http, integrity, failures: {total, items, next_cursor}}`. `http.since` обозначает начало текущего процесса; эти счётчики не фильтруются историческим периодом. Источники сбоев HTTP/OPERATION/GENERATION/SIP, без персональных и учебных полей. `integrity.status`: OK/FAIL/UNAVAILABLE/EMPTY. Неверный период/курсор — 400 VALIDATION_ERROR. `export=true` игнорирует пагинацию и возвращает JSON-вложение со всеми сбоями периода; более 10 000 — явная 400 VALIDATION_ERROR. Ответ private/no-store; просмотр/экспорт аудируются. Подробности полей и границ проверок — docs/DIAGNOSTICS.md.

Для всех `backup_*` значение service — `backup`. `backup_create` создаёт полную копию. `backup_verify` требует существующий `snapshot` вида `backup-YYYYMMDDTHHMMSSZ-xxxxxxxxxxxx` и восстанавливает его файлы и БД в отдельный временный каталог и проверочную БД. `backup_configure` требует `schedule: {enabled, hour: 0..23, minute: 0..59, retention: 14..90}`; время московское, значения строго типизированы. Лишний snapshot/schedule для другого вида операции отклоняется. Неверные поля — 400 `VALIDATION_ERROR`, отсутствующая копия — 404 `NOT_FOUND`, отсутствие/неготовность исполнителя или неверная подпись — 503 `BACKUP_UNAVAILABLE`.

Исполнитель backup получает только подписанные файловые задания с фиксированными операциями, без Docker socket и произвольных команд. Запросы, результаты, каталог, расписание и результаты восстановления подписаны отдельным ключом `.secrets/backup-control/token`. Перезапуск помечает прерванное RUNNING-задание как FAILED; оно не запускается повторно. Проверка восстановления сохраняет основную БД. Аварийное переключение рабочей программы на копию ещё не реализовано этим API и остаётся в этапе 15.

При недоступном техническом контроллере — 503 `CONTROL_UNAVAILABLE`. Контроллер пока выключен до разрешения запуска; это ограничение текущей приёмки, а не заглушка успешного ответа.

Отдельный установщик восстановления (`scripts/recover_snapshot.py`, `docs/RECOVERY.md`) разворачивает снимок в новый экземпляр с обслуживанием и новым ключом JWT. Пока `system_settings.recovery.status = PREPARED`, `POST /admin/maintenance` с `enabled: false` возвращает 409 `RECOVERY_NOT_ACTIVATED`; открыть учебные изменения можно после проверенной активации установщиком. События `SYSTEM_RECOVERY_PREPARED`, `SYSTEM_RECOVERY_INTERRUPTED`, `SYSTEM_RECOVERY_ACTIVATED` входят в техническое представление аудита. REST-пульт не получает Docker socket или возможность выполнять команды установщика.

Внутренний протокол обновления ПО использует `system_settings.software_update` и `maintenance.update_id`. Пока обновление владеет обслуживанием, изменение `POST /admin/maintenance` и новая `POST /admin/operations/jobs` возвращают 409 `OPERATION_IN_PROGRESS`. Учебные изменения продолжают возвращать 503 `MAINTENANCE`. События `SOFTWARE_UPDATE_*` доступны в техническом представлении аудита. Применение выполняет локальный установщик с резервированием и миграциями; административный API готовит подписанные запросы и показывает фактические события установки (docs/UPDATES.md).

### 9.1. Пакеты программы и запросы установщику

Все маршруты ниже имеют префикс `/api/v1/admin/operations/updates`, требуют действующего ADMIN и CSRF при изменениях. Это не интерфейс выполнения произвольных команд. Преподаватель и обучающийся получают 403.

- `GET ""`: `trust {configured,fingerprint,error}`, `limits {archive_bytes,packages,storage_bytes}`, `packages`, последние `updates`, `current`, `maintenance`, `execution: LOCAL_COMMAND`. Фазы берутся из БД и аудита, не вычисляются по факту загрузки архива.
- `POST /packages?filename=release.zip`: тело — исходные байты ZIP, `Content-Type: application/octet-stream`; ответ 201 с `{id,filename,version,description,release_created_at,uploaded_at,sha256,size,publisher_sha256,program_files,image_services}`. Проверяются все файлы и Ed25519-подпись. Предел архива — 264 МиБ с учётом служебных данных, каталог — 20 пакетов / 2 ГиБ. Повтор того же исправного архива возвращает прежнюю запись. Изменённый или недоступный архив повтором не скрывается.
- `GET /packages/{id}`: скачивание с повторной проверкой подписи и хешей. Имя загрузки — `release-VERSION.zip`.
- `DELETE /packages/{id}`: 204; удаляет только загруженный архив, не установленную программу. Активная техническая операция или действующий запрос на пакет запрещает удаление. Событие остаётся в аудите.
- `POST /requests`: `{id: UUID, action: apply|activate|rollback, package_id?: UUID|null, update_id?: UUID|null, reason: string(5..1000)}`. Для apply нужен package_id без update_id; для остальных — существующий update_id без package_id. Activate допускается только в READY, rollback — до ACTIVE. Ответ `{request:{value,signature},filename,execution:LOCAL_COMMAND}` — файл для отдельного локального установщика, а не результат установки. Повтор id с теми же параметрами возвращает тот же запрос; изменение параметров — 409 IDEMPOTENCY_CONFLICT.

Подписанное value формата `software-update-request-1` содержит schema, id, update_id, action, actor_id, package_id, package_sha256, publisher_sha256, version, reason, created_at, expires_at. Срок — ровно 24 часа; пути и команды клиента не принимаются. Ключ HMAC выводится из backup-control и JWT_SECRET с отдельным назначением software-update-request-key-1: восстановление меняет JWT_SECRET, поэтому унаследованный ключ резервирования не позволяет переносить запрос между исходной и восстановленной установками. Смена ключа сессий требует нового запроса. Серверный открытый ключ издателя читается только из подготовленного `/run/software-publisher.pub`; UI не меняет источник доверия. Локальный execute-request повторно проверяет подпись запроса, срок, издателя, точные байты пакета и действующие права в БД. После начала операции применяются фазы STARTED/BACKED_UP/MIGRATED/READY/ACTIVE либо ROLLING_BACK/ROLLED_BACK/FAILED; до запуска интерфейс показывает REQUESTED, после истечения невыполненного запроса — EXPIRED.

Ошибки: 400 UPDATE_PACKAGE_INVALID/VALIDATION_ERROR; 409 UPDATE_KEY_REQUIRED, UPDATE_KEY_CHANGED, UPDATE_PACKAGE_INVALID, UPDATE_STORAGE_FULL, UPDATE_REQUEST_UNAVAILABLE, OPERATION_IN_PROGRESS, INVALID_TRANSITION, IDEMPOTENCY_CONFLICT; 413 UPLOAD_TOO_LARGE; 503 UPDATE_KEY_UNAVAILABLE/UPDATE_STORAGE_UNAVAILABLE. Действия записываются как SOFTWARE_UPDATE_PACKAGE_VERIFIED/REJECTED/REMOVED и SOFTWARE_UPDATE_REQUESTED. Архивы хранятся в отдельном скрытом подкаталоге существующего записываемого тома материалов, метаданные — в system_settings с точным префиксом software_package:. Доступ к ним не предоставляется учебным маршрутам.

### 9.2. Эксплуатационные параметры

`GET /api/v1/admin/operations/configuration` требует ADMIN, возвращает `snapshot {revision,configuration,request_id,actor_id,reason,changed_at}`, `configured`, `maintenance`, `database_connection_budget`, `processes`, `missing`, `applied`. Отсутствующая сохранённая строка означает редакцию 0 с исходными значениями. В processes перечислены свежие подтверждения: `{id,role,revision,at,error,pool_size,checked_out,statement_timeout_ms,lock_timeout_ms}`. Роли — backend, worker, sip_worker; подтверждения старше 10 секунд и из будущего не учитываются.

`POST` по тому же адресу принимает `{id:UUID,expected_revision:int>=0,reason:string(5..1000),configuration:{database,sip,logging}}`. Имена полей, исходные значения, диапазоны и отношения приведены в docs/RUNTIME-CONFIGURATION.md и схеме `app/domain/runtime_configuration.py`; неизвестные поля запрещены, числа и флаги имеют строгие типы. Нужны ADMIN, CSRF, включённое обслуживание, отсутствие активных занятий/генераций/SIP-вызовов и технического владельца обслуживания. Ответ содержит поля GET и `saved_revision`, `replayed`, `superseded`. Повтор прежнего запроса после более нового изменения возвращает последнюю редакцию и `superseded:true`, не откатывая значения.

Ошибки: 409 `MAINTENANCE_REQUIRED`, `OPERATION_IN_PROGRESS`, `CONFIGURATION_CONFLICT`, `IDEMPOTENCY_CONFLICT`; 400 `CONFIGURATION_CAPACITY` при превышении бюджета соединений, `VALIDATION_ERROR` при неверной схеме; 503 `CONFIGURATION_STATUS_UNAVAILABLE` при невозможности проверить подтверждения. Сохранение и `RUNTIME_CONFIGURATION_CHANGED` атомарны. Если ответ потерян после commit, повторяется прежний id. После изменения параметров `POST /admin/maintenance` с `enabled:false` возвращает 409 `CONFIGURATION_NOT_APPLIED`, пока все процессы не подтвердят актуальную редакцию; при недоступной проверке — 503.

### 9.3. Проверка файлов и эталон

`GET /api/v1/admin/diagnostics/files` (ADMIN) возвращает актуальный подписанный результат проверки файлов. Новые типы существующих технических операций: `backup_integrity_check`, `backup_integrity_baseline`; выбор эталона требует имени полной копии, подтверждённого восстановления и причины. Форматы, состояния, ограничения свежести и ошибки описаны в [FILE-INTEGRITY.md](docs/FILE-INTEGRITY.md). Подпись или исполнитель недоступны — 503 `INTEGRITY_UNAVAILABLE`; непроверенный эталон — 409 `BASELINE_NOT_VERIFIED`. Результат проверки отделён от статуса выполнения операции.
## Дополнения 27.09.2026: локальные операции, масштабирование и обмен

- POST `/admin/operations/jobs/{job_id}/local-request` — подписанный запрос локального выполнения ожидающей операции; POST `/admin/operations/jobs/{job_id}/cancel` — отмена до начала. ADMIN, CSRF и существующая запись операции обязательны.
- GET `/admin/operations/recovery` — состояние переключения и UUID оператора; обслуживание нельзя снять при `switch_id` или `topology_id`.
- GET `/admin/operations/configuration/export.xml` — выгрузка параметров без секретов; POST `/admin/operations/configuration/import.xml` принимает UTF-8 XML до 64 КиБ и возвращает проверенный черновик, не применяя его. DTD/сущности, повторные/неизвестные поля и недопустимые значения отвергаются.
- GET `/admin/workstations.xml` — XML рабочих мест (ADMIN).
- GET `/teacher/materials/{material_id}/export.xml` — структурированный материал собственного преподавателя.
- GET `/teacher/lessons/{lesson_id}/report.csv`, `.xml` — отчёт своего занятия; CSV — UTF-8 BOM и разделитель `;`, канонические колонки REPORT_COLUMNS.
- GET `/teacher/lessons/{lesson_id}/students/{student_id}/certificate.pdf` — подтверждение участия после FINISHED; чужие занятия/участники закрыты.

В XML применяются канонические JSON-имена полей, списки содержат элементы `item`, null отмечается `nil="true"`. Корневые элементы: `runtime_configuration`, `workstations`, `learning_material`, `lesson_report`, версия `schema="1"`. Операции экспорта записываются в аудит. Общие события backend передаются через Redis Stream, после переподключения клиент получает актуальный снимок БД.

Локальные ответы `/admin/operations/services` и журналов содержат `collected_at`, `stale` и `refresh_interval_sec` (null для разового сбора, 5–60 для монитора). Устаревание — три интервала непрерывного сбора либо пять минут для разового снимка. `applied` параметров требует подтверждения каждого настроенного backend; `missing` содержит `backend`, если подтверждений меньше `backend_replicas`. Это также блокирует снятие обслуживания после масштабирования, даже при исходной редакции параметров.
`GET /api/v1/teacher/lessons/{lesson_id}/report.xlsx` — Excel с теми же семью каноническими колонками и отметкой ручного балла; TEACHER только своего занятия, аудит LESSON_REPORT_EXPORTED с format=XLSX, no-store. Текст никогда не экспортируется как формула.
`GET /api/v1/sip/calls/{call_id}/recording?format=mp3` — локальная MP3-выгрузка завершённого разговора для участника или его преподавателя; format=wav по умолчанию. 404 при отсутствии/чужой записи, 400 при неизвестном формате, 503 PHONE_UNAVAILABLE при ошибке преобразования; private/no-store.
