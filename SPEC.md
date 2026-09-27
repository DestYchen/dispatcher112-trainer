# SPEC.md — Тренажёр диспетчера ДДС

Спецификация для реализации MVP-1. Читать вместе с `API.md`, `UI.md`, `PLAN.md`.

---

## 0. Как пользоваться этим документом

Документ нормативный. Формулировки:

- **MUST** — обязательно, без этого MVP не принимается
- **SHOULD** — сильно желательно
- **MAY** — на усмотрение реализации

Все идентификаторы, имена таблиц, полей, эндпойнтов и статусов в документе — канонические. Не переименовывать.

---

## 1. Что строим

Учебный симулятор рабочего места диспетчера дежурно-диспетчерской службы (АРМ-112). Обучающийся получает карточки происшествий, обрабатывает их в регламентные сроки, проставляет статусы реагирования, пишет комментарии и докладывает по симулятору телефонии. Система автоматически оценивает действия и формирует отчёт преподавателю.

### 1.1. Кто пользователи

| Роль | Код | Что делает |
| --- | --- | --- |
| Администратор | `ADMIN` | Пользователи, справочники, импорт классификатора, мониторинг |
| Преподаватель | `TEACHER` | Готовит занятие, раздаёт задания, наблюдает, завершает, смотрит отчёт |
| Обучающийся | `STUDENT` | Обрабатывает карточки |

### 1.2. Ключевые ограничения

- **Полностью локальный контур.** Никаких внешних HTTP-запросов в рантайме. Никаких CDN, никаких облачных API, шрифты и все ассеты — локально в репозитории.
- **Работа без GPU обязательна.** GPU — опциональный ускоритель за флагом конфигурации.
- **Русский язык интерфейса.** Локализация не требуется, но строки UI вынести в один модуль.

---

## 2. Архитектура

### 2.1. Компоненты

```
┌───────────────────────────────────────────────────────┐
│  Frontend (React SPA)                                 │
│  ├─ /student   рабочее место обучающегося             │
│  ├─ /teacher   пульт преподавателя                    │
│  └─ /admin     администрирование                      │
└──────────────┬────────────────────┬───────────────────┘
               │ REST               │ WebSocket
┌──────────────▼────────────────────▼───────────────────┐
│  Backend (FastAPI)                                    │
│  ├─ api/          роутеры                             │
│  ├─ domain/       бизнес-логика, автоматы             │
│  ├─ scoring/      движок оценки                       │
│  ├─ generation/   генерация сценариев (offline)       │
│  ├─ realtime/     WebSocket-хаб, таймеры              │
│  └─ db/           модели, репозитории, миграции       │
└──────────────┬────────────────────────────────────────┘
               │
┌──────────────▼────────────────────────────────────────┐
│  PostgreSQL 15                                        │
└───────────────────────────────────────────────────────┘
        │
┌───────▼──────────┐  ┌──────────────────┐
│ LanguageTool     │  │ Worker (RQ/arq)  │
│ (локальный, JVM) │  │ генерация, отчёты│
└──────────────────┘  └──────────────────┘
```

### 2.2. Стек

| Слой | Технология | Версия |
| --- | --- | --- |
| Backend | Python + FastAPI | 3.11+, FastAPI 0.110+ |
| ORM | SQLAlchemy 2.0 (async) + Alembic | |
| Валидация | Pydantic v2 | |
| Очередь | arq (Redis) | |
| БД | PostgreSQL | 15 |
| Frontend | React + TypeScript + Vite | React 18 |
| Роутинг | React Router | 6 |
| Состояние | TanStack Query + Zustand | |
| Стили | CSS Modules + CSS-переменные | без Tailwind |
| Реальное время | WebSocket (нативный) | |
| Орфография | LanguageTool self-hosted | |
| Тесты | pytest + pytest-asyncio, Vitest, Playwright | |
| Запуск | docker-compose | |

**Запрещено:** любые пакеты, требующие сетевого доступа в рантайме; UI-киты типа MUI/AntD (интерфейс должен повторять АРМ-112, а не выглядеть как SaaS).

### 2.3. Структура репозитория

```
/
├── docker-compose.yml
├── README.md
├── Makefile
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/
│   │   │   ├── deps.py
│   │   │   ├── auth.py
│   │   │   ├── admin.py
│   │   │   ├── teacher.py
│   │   │   ├── student.py
│   │   │   └── ws.py
│   │   ├── domain/
│   │   │   ├── enums.py
│   │   │   ├── status_machine.py
│   │   │   ├── classifier.py
│   │   │   └── session.py
│   │   ├── scoring/
│   │   │   ├── engine.py
│   │   │   ├── rules/
│   │   │   └── grammar.py
│   │   ├── generation/
│   │   │   ├── builder.py
│   │   │   └── llm.py
│   │   ├── realtime/
│   │   │   ├── hub.py
│   │   │   └── clock.py
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── models/
│   │   │   └── repos/
│   │   └── seeds/
│   │       ├── import_classifier.py
│   │       └── import_tickets.py
│   └── tests/
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── public/fonts/
│   └── src/
│       ├── main.tsx
│       ├── styles/tokens.css
│       ├── api/
│       ├── components/
│       ├── pages/
│       │   ├── student/
│       │   ├── teacher/
│       │   └── admin/
│       ├── hooks/
│       └── lib/
└── data/
    ├── classifier.xlsx
    ├── tickets/
    └── streets.csv
```

---

## 3. Модель данных

Все таблицы: `id` — `UUID` (`gen_random_uuid()`), `created_at`/`updated_at` — `TIMESTAMPTZ NOT NULL DEFAULT now()`. Мягкое удаление не используется, кроме `users.is_active`.

### 3.1. Пользователи и доступ

```sql
CREATE TYPE user_role AS ENUM ('ADMIN', 'TEACHER', 'STUDENT');

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    login         VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,        -- argon2id
    last_name     VARCHAR(100) NOT NULL,
    first_name    VARCHAR(100) NOT NULL,
    middle_name   VARCHAR(100),
    role          user_role NOT NULL,
    service_id    UUID REFERENCES services(id), -- к какой ДДС приписан студент
    totp_secret   VARCHAR(64),                  -- второй фактор, опционально
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workstations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    number     VARCHAR(16) NOT NULL UNIQUE,     -- «АРМ-07»
    room       VARCHAR(64),
    is_active  BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES users(id),
    action      VARCHAR(64) NOT NULL,           -- 'LOGIN', 'STATUS_SET', 'SCORE_OVERRIDE', ...
    entity_type VARCHAR(64),
    entity_id   UUID,
    payload     JSONB,
    ip          INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (created_at DESC);
CREATE INDEX ON audit_log (user_id, created_at DESC);
```

**MUST:** каждое действие пользователя, изменяющее состояние, пишет строку в `audit_log`. Записи не редактируются и не удаляются.

### 3.2. Классификатор

```sql
CREATE TABLE services (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code       VARCHAR(32) NOT NULL UNIQUE,     -- 'MCHS', 'MVD', 'MOSGAZ'
    name       VARCHAR(255) NOT NULL,           -- 'Мосгаз'
    short_name VARCHAR(64) NOT NULL,
    is_visible BOOLEAN NOT NULL DEFAULT true,   -- false = информационная, в списке не показывается
    sort_order INT NOT NULL DEFAULT 0
);

CREATE TABLE incident_groups (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code       VARCHAR(16) NOT NULL UNIQUE,
    name       VARCHAR(255) NOT NULL,           -- 'Пожары и задымления'
    sort_order INT NOT NULL DEFAULT 0
);

CREATE TABLE incident_types (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id      UUID NOT NULL REFERENCES incident_groups(id),
    code          VARCHAR(32) NOT NULL UNIQUE,  -- иерархический код из классификатора
    name          VARCHAR(500) NOT NULL,        -- 'пожар: мусор'
    attributes    JSONB NOT NULL,               -- {"level1":"на улице","level2":"мусор","level3":"открытое пламя"}
    difficulty    SMALLINT NOT NULL DEFAULT 5   -- 1..10
        CHECK (difficulty BETWEEN 1 AND 10)
);
CREATE INDEX ON incident_types USING GIN (attributes);

-- какие службы оповещаются по типу
CREATE TABLE incident_type_services (
    incident_type_id UUID NOT NULL REFERENCES incident_types(id) ON DELETE CASCADE,
    service_id       UUID NOT NULL REFERENCES services(id),
    PRIMARY KEY (incident_type_id, service_id)
);

-- модификаторы, меняющие состав оповещения
CREATE TYPE modifier_code AS ENUM (
    'THREAT_TO_PEOPLE', 'VICTIMS', 'FATALITIES',
    'NO_ACCESS', 'ROAD_BLOCKED', 'CHILD_INVOLVED'
);

CREATE TABLE modifier_services (
    modifier    modifier_code NOT NULL,
    service_id  UUID NOT NULL REFERENCES services(id),
    PRIMARY KEY (modifier, service_id)
);

CREATE TABLE streets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    name_norm   VARCHAR(255) NOT NULL,          -- нижний регистр, без ё
    district    VARCHAR(128)
);
CREATE INDEX ON streets USING GIN (name_norm gin_trgm_ops);
```

**Импорт:** `app/seeds/import_classifier.py` читает `data/classifier.xlsx` и наполняет `incident_groups`, `incident_types`, `services`, `incident_type_services`. Идемпотентен: повторный запуск обновляет, не дублирует.

**MUST:** `streets` заполняется из `data/streets.csv` — локального справочника улиц Москвы. Файл должен содержать пары-ловушки (Дубнинская / Дубининская) для проверки логики похожих названий.

### 3.3. Учебные сценарии

Сценарий — заготовка карточки с эталонным решением. Создаётся до занятия.

```sql
CREATE TYPE scenario_source AS ENUM ('TICKET', 'GENERATED', 'MANUAL');
CREATE TYPE scenario_status AS ENUM ('DRAFT', 'PENDING_REVIEW', 'APPROVED', 'REJECTED');
CREATE TYPE card_origin AS ENUM ('OPERATOR_112', 'EXTERNAL_SYSTEM');

CREATE TABLE scenarios (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title            VARCHAR(255) NOT NULL,
    source           scenario_source NOT NULL,
    status           scenario_status NOT NULL DEFAULT 'DRAFT',
    origin           card_origin NOT NULL DEFAULT 'OPERATOR_112',
    incident_type_id UUID NOT NULL REFERENCES incident_types(id),
    difficulty       SMALLINT NOT NULL CHECK (difficulty BETWEEN 1 AND 10),

    -- содержимое карточки, которую увидит студент
    card_payload     JSONB NOT NULL,

    -- эталон
    reference        JSONB NOT NULL,

    author_id        UUID REFERENCES users(id),
    approved_by      UUID REFERENCES users(id),
    approved_at      TIMESTAMPTZ,
    review_comment   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON scenarios (status, difficulty);
```

**`card_payload`** — то, что показывается студенту:

```json
{
  "card_number": "2026-0917-004412",
  "registered_at": "2026-09-17T14:22:05+03:00",
  "operator_workstation": "ОП-034",
  "applicant": { "name": "Иванова И. С.", "phone": "+7 916 ***-**-71" },
  "address": {
    "raw": "ул. Станционная, д. 28",
    "clarification": "при уточнении адреса — г. Королёв, МО",
    "lat": 55.9142, "lon": 37.8258
  },
  "attributes": ["на улице", "частный дом", "открытое пламя"],
  "incident_type_name": "пожар: частный дом",
  "modifiers": ["THREAT_TO_PEOPLE"],
  "description": "Горит крыша частного дома, пострадавших нет, дом не газифицирован.",
  "notified_services": ["MCHS", "MVD", "SMP"]
}
```

Для `origin = EXTERNAL_SYSTEM` поля `operator_workstation`, `attributes`, `modifiers` **MUST** быть пустыми или отсутствовать — карточка беднее, критичные детали только в `description`.

**`reference`** — эталонное решение:

```json
{
  "expected_status": "ACCEPTED",
  "expected_status_chain": ["ACCEPTED", "RESPONSE_STARTED", "ARRIVED", "WORK_IN_PROGRESS", "WORK_COMPLETED"],
  "comment_required": false,
  "comment_must_contain": [],
  "report_required": true,
  "report_callee_code": "DUTY_OFFICER",
  "report_must_mention": ["address", "incident_type", "victims"],
  "rationale": "Профильное происшествие для ДДС района. Отказ неправомерен.",
  "trap": null
}
```

Поле `trap` описывает ловушку сценария, если она есть: `"DUPLICATE_CARD"`, `"NOT_OUR_SERVICE"`, `"DETAIL_IN_DESCRIPTION"`, `"AMBIGUOUS_ADDRESS"`. Используется в разборе ошибок.

### 3.4. Занятия

```sql
CREATE TYPE lesson_status AS ENUM ('PLANNED', 'RUNNING', 'FINISHED');

CREATE TABLE lessons (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title         VARCHAR(255) NOT NULL,
    teacher_id    UUID NOT NULL REFERENCES users(id),
    status        lesson_status NOT NULL DEFAULT 'PLANNED',
    settings      JSONB NOT NULL,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE lesson_participants (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_id      UUID NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    student_id     UUID NOT NULL REFERENCES users(id),
    workstation_id UUID REFERENCES workstations(id),
    joined_at      TIMESTAMPTZ,
    UNIQUE (lesson_id, student_id)
);
```

**`lessons.settings`:**

```json
{
  "primary_status_deadline_sec": 30,
  "card_processing_deadline_sec": 180,
  "max_concurrent_cards": 3,
  "card_interval_sec": 45,
  "difficulty_range": [1, 6],
  "incident_group_ids": ["..."],
  "scenario_mode": "GENERATED",
  "grammar_check_enabled": true,
  "hints_enabled": false,
  "weights": {
    "timeliness": 0.30,
    "correctness": 0.40,
    "completeness": 0.20,
    "literacy": 0.10
  }
}
```

`scenario_mode`: `GENERATED` | `TICKETS` | `MIXED`.

### 3.5. Задания и попытки

`assignment` — экземпляр сценария, выданный конкретному студенту в конкретном занятии.

```sql
CREATE TYPE assignment_state AS ENUM (
    'QUEUED', 'DELIVERED', 'OPENED', 'PRIMARY_SET', 'CLOSED', 'EXPIRED'
);

CREATE TABLE assignments (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_id          UUID NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    student_id         UUID NOT NULL REFERENCES users(id),
    scenario_id        UUID NOT NULL REFERENCES scenarios(id),
    card_number        VARCHAR(32) NOT NULL,
    state              assignment_state NOT NULL DEFAULT 'QUEUED',

    delivered_at       TIMESTAMPTZ,   -- t0 для дедлайна 30 с
    opened_at          TIMESTAMPTZ,   -- t0 для дедлайна 3 мин
    primary_status_at  TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,

    score              JSONB,         -- результат движка оценки
    teacher_override   JSONB,         -- ручная правка оценки преподавателем

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON assignments (lesson_id, student_id);
CREATE INDEX ON assignments (state) WHERE state IN ('DELIVERED','OPENED','PRIMARY_SET');
```

### 3.6. Действия студента

Полный журнал. Источник истины для оценки и разбора.

```sql
CREATE TYPE response_status AS ENUM (
    'ADDED', 'RECEIVED',
    'ACCEPTED', 'NOT_ACCEPTED',
    'RESPONSE_STARTED', 'ARRIVED', 'WORK_IN_PROGRESS',
    'WORK_COMPLETED', 'WORK_REFUSED'
);

CREATE TABLE status_events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id  UUID NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    status         response_status NOT NULL,
    comment        TEXT,
    is_automatic   BOOLEAN NOT NULL DEFAULT false,
    elapsed_ms     INT NOT NULL,     -- от delivered_at
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON status_events (assignment_id, created_at);

CREATE TABLE interaction_events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id  UUID NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    kind           VARCHAR(48) NOT NULL,  -- 'CARD_OPENED','DESCRIPTION_SCROLLED','MAP_OPENED','SEARCH_USED'
    payload        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE phone_reports (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id  UUID NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    callee_code    VARCHAR(32) NOT NULL,
    dialed_number  VARCHAR(16) NOT NULL,
    transcript     TEXT,                  -- расшифровка или текстовый ввод
    duration_ms    INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE directory_entries (       -- телефонный справочник симулятора
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        VARCHAR(32) NOT NULL UNIQUE,  -- 'DUTY_OFFICER'
    number      VARCHAR(16) NOT NULL UNIQUE,  -- '2201'
    title       VARCHAR(255) NOT NULL,        -- 'Оперативный дежурный'
    voice       VARCHAR(32) NOT NULL,         -- 'male_calm', 'female_brisk'
    greeting    TEXT NOT NULL DEFAULT 'Слушаю вас.',
    confirmation TEXT NOT NULL DEFAULT 'Я вас понял, информация принята.'
);
```

---

## 4. Автомат статусов

**MUST** реализовать в `domain/status_machine.py` как чистую функцию без обращений к БД, покрытую юнит-тестами.

### 4.1. Граф переходов

```
              ┌──────────┐
   (система)  │  ADDED   │
              └────┬─────┘
                   │ (система, при открытии карточки)
              ┌────▼─────┐
              │ RECEIVED │
              └────┬─────┘
          ┌────────┴────────┐
    ┌─────▼──────┐   ┌──────▼────────┐
    │  ACCEPTED  │◄──┤ NOT_ACCEPTED  │
    └─────┬──────┘   └───────────────┘
          │            (комментарий обязателен)
    ┌─────┴───────────────────────────┐
    │                                 │
┌───▼──────────────┐         ┌────────▼────────┐
│ RESPONSE_STARTED │         │  WORK_REFUSED   │ ← терминальный
└───┬──────────────┘         └─────────────────┘   (комментарий обязателен)
    │
┌───▼──────┐
│ ARRIVED  │
└───┬──────┘
    │
┌───▼───────────────┐
│ WORK_IN_PROGRESS  │
└───┬───────────────┘
    │
┌───▼────────────┐
│ WORK_COMPLETED │ ← терминальный
└────────────────┘
```

### 4.2. Таблица переходов

| Текущий | Доступные далее |
| --- | --- |
| `ADDED` | `RECEIVED` (только система) |
| `RECEIVED` | `ACCEPTED`, `NOT_ACCEPTED` |
| `NOT_ACCEPTED` | `ACCEPTED` |
| `ACCEPTED` | `RESPONSE_STARTED`, `ARRIVED`, `WORK_IN_PROGRESS`, `WORK_COMPLETED`, `WORK_REFUSED` |
| `RESPONSE_STARTED` | `ARRIVED`, `WORK_IN_PROGRESS`, `WORK_COMPLETED`, `WORK_REFUSED` |
| `ARRIVED` | `WORK_IN_PROGRESS`, `WORK_COMPLETED`, `WORK_REFUSED` |
| `WORK_IN_PROGRESS` | `WORK_COMPLETED`, `WORK_REFUSED` |
| `WORK_COMPLETED` | — (терминальный) |
| `WORK_REFUSED` | — (терминальный) |

### 4.3. Правила

1. **MUST** Переход, отсутствующий в таблице, отклоняется с `409 INVALID_TRANSITION`. Фронтенд не показывает недоступные варианты, но бэкенд проверяет независимо.
2. **MUST** `NOT_ACCEPTED` и `WORK_REFUSED` требуют непустой комментарий длиной ≥ 15 символов. Иначе `422 COMMENT_REQUIRED`.
3. **MUST** Достижение `WORK_COMPLETED` или `WORK_REFUSED` переводит `assignment.state` в `CLOSED`, запрещает любые дальнейшие изменения и запускает оценку.
4. **MUST** Перед подтверждением `WORK_COMPLETED` фронтенд показывает модальное предупреждение: карточка закроется для редактирования.
5. **SHOULD** `ADDED` создаётся автоматически при выдаче задания, `RECEIVED` — при первом открытии карточки.

---

## 5. Таймеры и многозадачность

### 5.1. Два дедлайна

| Дедлайн | По умолчанию | Отсчёт от | Нарушение |
| --- | --- | --- | --- |
| Первичный статус | 30 с | `delivered_at` | `assignment.state → EXPIRED`, карточка помечается «Не оповещено» |
| Обработка карточки | 180 с | `opened_at` | фиксируется превышение, карточка остаётся активной |

**MUST** Оба значения берутся из `lessons.settings`, не хардкодятся.

### 5.2. Источник времени

**MUST** Сервер — единственный источник истины по времени. Фронтенд получает `server_time` и `deadline_at` в ISO-8601 с таймзоной и отсчитывает локально, синхронизируясь при каждом WebSocket-сообщении. Оценка считается **только** по серверным меткам.

### 5.3. Выдача карточек

Планировщик в `realtime/clock.py`, тик 1 с:

```
для каждого активного занятия:
    для каждого участника:
        active = карточки в состоянии DELIVERED|OPENED|PRIMARY_SET
        если len(active) < settings.max_concurrent_cards
           и прошло >= settings.card_interval_sec с последней выдачи
           и в очереди есть QUEUED:
               выдать следующую → DELIVERED, push по WebSocket
    для каждой DELIVERED-карточки:
        если now - delivered_at > primary_status_deadline и нет первичного статуса:
            → EXPIRED, push по WebSocket
```

**MUST** Просроченная карточка не исчезает: остаётся в списке с красной индикацией, студент может её обработать, но нарушение уже зафиксировано.

---

## 6. Движок оценки

`scoring/engine.py`. **MUST** детерминирован: одинаковый набор событий → одинаковый результат. Никаких вызовов LLM.

### 6.1. Четыре оси

Соответствуют правилам корректности из памятки АРМ-112.

| Ось | Код | Вес по умолчанию | Что проверяет |
| --- | --- | --- | --- |
| Своевременность | `timeliness` | 0.30 | Укладывание в 30 с и 180 с |
| Правильность | `correctness` | 0.40 | Статус соответствует фактической ситуации и компетенции службы |
| Полнота | `completeness` | 0.20 | Обязательные комментарии, наличие ключевых сущностей, доклад |
| Грамотность | `literacy` | 0.10 | Орфография, критичность ошибок |

Итог: `total = Σ(ось_балл × вес)`, в диапазоне 0…100.

### 6.2. Правила по осям

**timeliness**

```
primary_delay = primary_status_at - delivered_at
if primary_delay <= deadline:          100
elif primary_delay <= deadline * 2:    линейно 100 → 40
else:                                  0

processing_time = closed_at - opened_at
аналогично относительно card_processing_deadline

timeliness = 0.6 * primary_score + 0.4 * processing_score
```

**correctness**

| Проверка | Вес внутри оси | Нарушение |
| --- | --- | --- |
| Первичный статус совпал с `expected_status` | 0.50 | `WRONG_PRIMARY_STATUS` |
| Не было отказа от профильного происшествия | 0.25 | `REFUSED_OWN_INCIDENT` (критическая) |
| Последовательность статусов без пропусков ключевых этапов | 0.15 | `SKIPPED_PROGRESS_STATUS` |
| Не поставлен `ACCEPTED` при фактическом непроведении работ и наоборот | 0.10 | `STATUS_MISMATCH` |

**completeness**

| Проверка | Вес | Нарушение |
| --- | --- | --- |
| Обязательный комментарий присутствует | 0.40 | `MISSING_COMMENT` (критическая) |
| Комментарий содержит все `comment_must_contain` | 0.35 | `INCOMPLETE_COMMENT` |
| Доклад по телефонии сделан, если `report_required` | 0.25 | `MISSING_REPORT` |

`comment_must_contain` — список семантических требований: `"reason"`, `"handed_to"`, `"card_number"`, `"clarified_address"`. Проверка **MUST** быть комбинированной:

1. Регулярные выражения и ключевые слова по словарю синонимов (`scoring/rules/lexicon.py`)
2. Проверка именованных сущностей: номер карточки — `\d{4}-\d{4}-\d{6}`, наименование организации — по справочнику `services` + список сторонних УК

LLM для этого **MUST NOT** использоваться в рантайме.

**literacy**

```
ошибки = LanguageTool(текст, язык='ru-RU')
критические = ошибки в полях адреса + ошибки в названиях улиц,
              не прошедшие сверку со справочником streets
некритические = остальные

literacy = max(0, 100 - 25 * критические - 5 * некритические)
```

### 6.3. Проверка адресов

**MUST** Отдельный модуль `scoring/rules/address.py`.

```
для каждой улицы, упомянутой в тексте:
    точное совпадение со streets.name_norm → ок
    иначе:
        кандидаты = trigram-поиск по streets, similarity > 0.7
        если кандидаты есть → критическая ошибка ADDRESS_TYPO,
                              в результат кладётся список кандидатов
        иначе → некритическая ошибка UNKNOWN_STREET
```

В UI это показывается подчёркиванием с подсказкой «Возможно, вы имели в виду: Дубнинская». Это ключевая фича, обоснованная реальным инцидентом заказчика.

### 6.4. Формат результата

`assignments.score`:

```json
{
  "total": 78.5,
  "axes": {
    "timeliness":   { "score": 92.0, "weight": 0.30 },
    "correctness":  { "score": 75.0, "weight": 0.40 },
    "completeness": { "score": 65.0, "weight": 0.20 },
    "literacy":     { "score": 90.0, "weight": 0.10 }
  },
  "violations": [
    {
      "code": "INCOMPLETE_COMMENT",
      "severity": "MAJOR",
      "axis": "completeness",
      "message": "В комментарии не указано, куда передана информация.",
      "hint": "К статусу «Не принята» укажите причину отказа и службу, в которую передана информация.",
      "field": "comment",
      "at": "2026-09-17T14:23:41+03:00"
    }
  ],
  "timings": {
    "primary_delay_ms": 24800,
    "processing_ms": 164200,
    "primary_deadline_ms": 30000,
    "processing_deadline_ms": 180000
  },
  "grammar": { "critical": 0, "minor": 2, "items": [] },
  "computed_at": "2026-09-17T14:25:02+03:00",
  "engine_version": "1.0.0"
}
```

`severity`: `CRITICAL` | `MAJOR` | `MINOR`.

**MUST** `engine_version` пишется в каждый результат, чтобы отчёты оставались воспроизводимыми.

### 6.5. Приоритет преподавателя

**MUST** Преподаватель может переопределить любую ось или итог. Переопределение пишется в `assignments.teacher_override`, оригинальная оценка не затирается, в `audit_log` пишется `SCORE_OVERRIDE`. В отчётах отображается итог преподавателя с пометкой о ручной корректировке.

---

## 7. Подсистема генерации

### 7.1. Принцип

**MUST** Генерация выполняется **вне занятия**, в фоновом воркере. В рантайме занятия модель не вызывается.

Обоснование: заказчик подтвердил CPU-only как базовую конфигурацию, а дедлайн отклика интерфейса — 2 секунды.

### 7.2. Пайплайн

```
1. SELECT     преподаватель выбирает группы происшествий и диапазон сложности
2. SAMPLE     случайная выборка строк классификатора в рамках фильтра
3. COMPOSE    для каждой строки формируется card_payload:
              - адрес из справочника streets (не генерируется!)
              - тип и признаки из классификатора
              - список служб вычисляется детерминированно
              - ФИО и телефон из генератора фейковых ПДн
              - описание: LLM или шаблон
4. REFERENCE  эталон вычисляется детерминированно из классификатора
5. VALIDATE   автопроверка непротиворечивости (см. 7.4)
6. REVIEW     статус PENDING_REVIEW, преподаватель подтверждает/правит
```

**MUST** Адреса, типы, названия служб и номера карточек **никогда** не приходят от LLM. LLM отвечает только за текст описания и реплики.

### 7.3. Интерфейс LLM

`generation/llm.py` — абстракция с двумя реализациями:

```python
class TextGenerator(Protocol):
    async def generate(self, prompt: str, *, max_tokens: int = 300) -> str: ...

class TemplateGenerator:   # fallback, без модели, всегда доступен
class LocalLLMGenerator:   # llama.cpp / Ollama, локально
```

Выбор через `config.GENERATION_BACKEND = "template" | "local_llm"`. **MUST** `template` работает без каких-либо моделей — MVP обязан запускаться на чистой машине.

### 7.4. Автовалидация сценария

Перед показом преподавателю:

| Проверка | Действие при провале |
| --- | --- |
| Улица есть в `streets` | отклонить |
| Признаки соответствуют выбранному типу | отклонить |
| Описание не противоречит модификаторам (нет «пострадавших нет» при `VICTIMS`) | пометить, отправить на ручную правку |
| Длина описания 40–400 символов | перегенерировать, до 3 попыток |
| Описание не содержит номеров телефонов и ФИО, кроме заданных | отклонить |

### 7.5. Правка преподавателем

**MUST** У преподавателя есть свободное текстовое поле для комментария к сценарию. Комментарий сохраняется в `scenarios.review_comment` и при перегенерации передаётся в промпт. Пометки преподавателя накапливаются в `data/feedback.jsonl` для последующего дообучения.

---

## 8. Симулятор телефонии

### 8.1. Направление

**MUST** Моделируется только **исходящий** вызов: студент → должностное лицо. Входящие звонки от заявителя в MVP-1 отсутствуют.

### 8.2. Механика

1. Студент открывает панель телефонии, набирает номер из справочника `directory_entries`
2. Проигрывается приветствие абонента (`greeting`)
3. Студент делает доклад — голосом (если включён STT) или текстом в поле
4. Проигрывается подтверждение (`confirmation`)
5. Запись сохраняется в `phone_reports`

### 8.3. Голоса

**MUST** Реплики абонентов синтезируются **заранее** и хранятся как WAV-файлы в `data/voices/{voice}/{phrase_id}.wav`. В рантайме — только воспроизведение.

Минимум четыре голоса: `male_calm`, `male_brisk`, `female_calm`, `female_brisk`.

### 8.4. Ввод доклада

MVP-1: **текстовый** ввод обязателен, голосовой опционален за флагом `STT_ENABLED`. При включённом STT — локальный Vosk, полудуплекс (кнопка «Говорить», удержание), не потоковое распознавание.

---

## 9. Нефункциональные требования

| Параметр | Значение | Как проверять |
| --- | --- | --- |
| Отклик API (p95) | < 300 мс | нагрузочный тест, 20 сессий |
| Отклик UI на действие | < 2 с | ручная проверка |
| Одновременных сессий | ≥ 20 | нагрузочный тест |
| Запись в БД | ≥ 100 оп/с | benchmark |
| Формирование отчёта | < 30 с | тест на занятии из 20 студентов × 10 карточек |
| Восстановление после разрыва сети | ≤ 30 с, без потери данных | WebSocket reconnect с backoff |

### 9.1. Безопасность

- **MUST** Пароли — argon2id, параметры по OWASP
- **MUST** Сессии — JWT в httpOnly-cookie, `SameSite=Strict`, TTL 8 ч
- **MUST** RBAC на уровне зависимостей FastAPI (`Depends(require_role(...))`), не только на фронтенде
- **MUST** Все мутирующие запросы — CSRF-токен
- **MUST** Rate limiting на `/auth/login`: 5 попыток / 15 мин на логин
- **SHOULD** TOTP как второй фактор (SMS/push недоступны в изолированном контуре)
- **MUST** Журналы безопасности хранятся ≥ 6 месяцев, ротация не удаляет раньше

### 9.2. Отказоустойчивость

- **MUST** WebSocket переподключается с экспоненциальным backoff, при восстановлении запрашивает полное состояние (`GET /student/state`)
- **MUST** Действия студента идемпотентны по `Idempotency-Key`
- **MUST** Ежесуточный `pg_dump` по cron в контейнере, хранение 14 копий

### 9.3. Наблюдаемость

- **MUST** Структурные логи в JSON, поля `ts`, `level`, `request_id`, `user_id`, `route`, `duration_ms`
- **SHOULD** `/healthz` и `/metrics` (Prometheus-формат)

---

## 10. Что НЕ входит в MVP-1

Зафиксировать явно, чтобы агент не расползался:

- Голосовая генерация входящего звонка от заявителя
- Двусторонний диалог с заявителем
- Дообучение моделей
- Горизонтальное масштабирование и кластер
- Экспорт в Excel (PDF достаточно)
- Адаптивная сложность на основе прогноза

Уточнение от 25.09.2026: первоначальное исключение мобильной вёрстки отменено запросом пользователя. Требуется адаптивность от 360 px, см. UI.md §3 и docs/CUSTOMER-SPEC-REVIEW.md.

## 11. Расширение после MVP-1

Запрос пользователя о завершении полного ТЗ из `данные UI/ТЗ_ДГОЧСиПБ_финал_01092026ГСИ.docx` расширяет первоначальную границу §10. Ограничения §8.1 «только исходящий» и §10 относятся к историческому MVP-1; дальнейшая реализация выполняется по PLAN.md этапам 12–17. Чистый автомат статусов §4, обязательные комментарии, серверные сроки, адресная проверка и аудит остаются обязательными для режима действий ДДС.

Для режима CARD_ENTRY в assignments добавлены `task_mode`, `card_draft`, `card_submission`, `draft_revision`, `submitted_at`; эталон остаётся в scenarios. Приём сообщения — первый норматив, заполнение — второй. Оценка воспроизводима (`entry-1.0.0`): правильность — доля совпавших полей из восьми, полнота — доля заполненных требуемых полей при сдаче (0 для несданной карточки); своевременность и грамотность используют прежние формулы. Сравнение нормализует регистр, ё, пунктуацию, порядок служб/модификаторов и обозначения улица/дом; разные улицы и номера домов не склеиваются. Описание сверяется с заданными преподавателем словами/фразами либо с нормализованным текстом эталона. Это проверяемый регламентный балл; отдельная ML-аналитика предусмотрена этапом 14.
## 12. Материалы и локальный ИИ (расширение этапа 14)

По позднему запросу о полном ТЗ добавлены learning_groups/group_members, learning_materials, learning_modules/module_assignments/module_progress, assignment_feedback, learning_models/learning_forecasts. Миграция a672c55c45a5 не меняет прежние данные. Учебные мутации аудируются; библиотека и аналитика имеют отдельную проверку преподавательской роли, учащийся получает только назначенный контент и собственную историю.

Регламентный движок остаётся детерминированным. Необязательные success_criteria дают отдельное заключение и сохраняются со снимком score. Нейросеть прогнозирует следующие показатели по истории, не участвует в оценке карточки. Локальная Qwen подготавливает текст до занятия; модель и лицензия загружаются на установочном шаге, рабочий контур изолирован. Архитектура, методика и ограничения приведены в docs/LEARNING-AI.md, контракты — API.md §8.
