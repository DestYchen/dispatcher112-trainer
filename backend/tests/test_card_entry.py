from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Scenario, User
from app.domain.card_entry import compare_fields
from app.domain.lesson_settings import LessonSettings
from app.realtime.clock import tick
from app.scoring.engine import evaluate
from app.scoring.types import ScoreInput
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


def reference_card() -> dict[str, Any]:
    return {
        "applicant": {"name": "Иванова Ирина", "phone": "+7 900 ***-**-11"},
        "address": {"raw": "Дубнинская улица, д. 28", "clarification": "Двор"},
        "incident_type_id": "fire",
        "description": "Горит крыша дома. Люди вышли на улицу.",
        "modifiers": ["THREAT_TO_PEOPLE"],
        "notified_services": ["MCHS", "MVD"],
    }


def test_entry_comparison_is_deterministic_and_normalizes_typography() -> None:
    expected = reference_card()
    answer = deepcopy(expected)
    answer["address"]["raw"] = "ул. Дубнинская, дом 28"
    answer["applicant"]["phone"] = "+7 (900) *** ** 11"
    answer["notified_services"].reverse()
    result = compare_fields(answer, expected, [])
    assert all(row["correct"] for row in result)
    assert all(compare_fields(answer, expected, []) == result for _ in range(100))


def test_address_split_across_fields_in_any_order_is_correct() -> None:
    expected = reference_card()
    answer = deepcopy(expected)
    answer["address"] = {"raw": "Москва, Дубнинская, д. 28", "clarification": "подъезд 1; двор"}
    rows = {row["field"]: row for row in compare_fields(answer, expected, [])}
    assert rows["address.raw"]["correct"] and rows["address.clarification"]["correct"]


@pytest.mark.parametrize(
    "field",
    [
        "applicant.name",
        "applicant.phone",
        "address.raw",
        "address.clarification",
        "incident_type_id",
        "description",
        "modifiers",
        "notified_services",
    ],
)
def test_every_entry_field_is_compared_and_missing_is_distinct(field: str) -> None:
    expected = reference_card()
    answer = deepcopy(expected)
    path = field.split(".")
    target = answer[path[0]] if len(path) == 2 else answer
    target[path[-1]] = [] if isinstance(target[path[-1]], list) else ""
    result = {row["field"]: row for row in compare_fields(answer, expected, [])}
    assert not result[field]["correct"] and not result[field]["present"]
    assert all(row["correct"] for key, row in result.items() if key != field)


def test_similar_street_wrong_house_negation_and_extra_service_are_errors() -> None:
    expected = reference_card()
    for field, wrong in [
        ("address.raw", "Дубининская улица, д. 28"),
        ("address.raw", "Дубнинская улица, д. 82"),
        ("description", "Не горит крыша дома. Люди вышли на улицу."),
    ]:
        answer = deepcopy(expected)
        if "." in field:
            answer["address"]["raw"] = wrong
        else:
            answer[field] = wrong
        row = next(row for row in compare_fields(answer, expected, []) if row["field"] == field)
        assert not row["correct"]
    answer = deepcopy(expected)
    answer["notified_services"].append("SMP")
    assert not compare_fields(answer, expected, [])[-1]["correct"]


def test_teacher_keywords_allow_paraphrase_but_all_are_required() -> None:
    expected = reference_card()
    answer = deepcopy(expected)
    answer["description"] = "На крыше открытое пламя. Люди покинули дом."
    keywords = ["пламя", "люди", "покинули"]
    rows = compare_fields(answer, expected, keywords)
    assert next(row for row in rows if row["field"] == "description")["correct"]
    answer["description"] = "Открытое пламя."
    rows = compare_fields(answer, expected, keywords)
    assert not next(row for row in rows if row["field"] == "description")["correct"]


def test_entry_score_one_hundred_identical_runs_and_server_deadlines() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    fields = tuple(compare_fields(reference_card(), reference_card(), []))
    data = ScoreInput(
        delivered_at=now,
        opened_at=now + timedelta(seconds=30),
        primary_status_at=now + timedelta(seconds=30),
        closed_at=now + timedelta(seconds=210),
        settings=LessonSettings().model_dump(mode="json"),
        reference={},
        events=(),
        entry_fields=fields,
        card_submitted=True,
    )
    score = evaluate(data)
    assert score["total"] == 100 and score["timings"]["processing_ms"] == 180000
    assert all(evaluate(data) == score for _ in range(100))


async def test_entry_full_cycle_hidden_reference_draft_conflict_submission_and_audit(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    row = rows[0]
    row.task_mode = "CARD_ENTRY"
    lesson.settings = {**lesson.settings, "grammar_check_enabled": False}
    scenario = await db.get(Scenario, row.scenario_id)
    assert scenario is not None
    scenario.card_payload = {**scenario.card_payload, "incoming_message": "Учебное обращение"}
    await db.flush()
    await tick(db, datetime.now(UTC) - timedelta(seconds=5))
    await sign_in(client)
    prefix = f"/api/v1/student/assignments/{row.id}"
    state = (await client.get("/api/v1/student/state")).json()
    assert state["cards"][0]["address_short"] == "Заполните по обращению"
    detail = (await client.get(prefix)).json()
    assert detail["task_mode"] == "CARD_ENTRY" and detail["my_block"]["available_statuses"] == []
    assert "Дубнинская" not in str(detail) and "СЕКРЕТ" not in str(detail)
    assert detail["entry"]["incoming_message"] == "Учебное обращение"
    assert row.primary_status_at == row.opened_at
    opened = row.opened_at
    await client.get(prefix)
    assert opened == row.opened_at
    assert (
        await client.post(
            prefix + "/status",
            json={"status": "ACCEPTED"},
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).status_code == 409
    card = {
        key: scenario.card_payload[key]
        for key in ("applicant", "address", "description", "modifiers", "notified_services")
    }
    # Invisible informational services are not selectable and are excluded from the rubric.
    card["notified_services"] = ["MCHS", "DDS_CHERTANOVO"]
    card["incident_type_id"] = str(scenario.incident_type_id)
    body = {"revision": 0, "card": card}
    headers = {"Idempotency-Key": str(uuid4())}
    saved = await client.post(prefix + "/draft", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert (await client.post(prefix + "/draft", json=body, headers=headers)).json() == saved.json()
    conflict = await client.post(
        prefix + "/draft", json=body, headers={"Idempotency-Key": str(uuid4())}
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "DRAFT_CONFLICT"
    assert (await client.get(prefix)).json()["entry"]["draft"] == saved.json()["draft"]
    submit = await client.post(prefix + "/submit-card", json={"revision": 1}, headers=headers)
    assert submit.status_code == 200, submit.text
    assert row.state == "CLOSED" and row.submitted_at and row.score
    assert row.score["total"] == 100
    assert row.score["engine_version"] == "entry-1.0.0"
    assert (
        await client.post(prefix + "/submit-card", json={"revision": 1}, headers=headers)
    ).json() == submit.json()
    assert (
        await client.post(
            prefix + "/draft",
            json={**body, "revision": 1},
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).status_code == 409
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "CARD_SUBMITTED", AuditLog.entity_id == row.id)
        )
        == 1
    )
    client.cookies.clear()
    await sign_in(client, "teacher", "teacher")
    reused = await client.post(f"/api/v1/teacher/assignments/{row.id}/reuse-card")
    assert reused.status_code == 201, reused.text
    assert (
        await client.post(f"/api/v1/teacher/assignments/{row.id}/reuse-card")
    ).json() == reused.json()
    clone = await db.get(Scenario, reused.json()["id"])
    assert clone is not None and clone.status == "PENDING_REVIEW"
    assert clone.card_payload["source_assignment_id"] == str(row.id)
    assert row.card_submission is not None
    assert clone.card_payload["address"] == row.card_submission["address"]
    original = deepcopy(row.card_submission)
    pdf = await client.get(f"/api/v1/teacher/lessons/{lesson.id}/report.pdf")
    assert pdf.status_code == 200
    pdf_text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "Заполнение карточки" in pdf_text and "Дубнинская" in pdf_text
    clone.card_payload = {**clone.card_payload, "description": "Отредактированный сценарий"}
    await db.flush()
    assert row.card_submission == original
    client.cookies.clear()
    owner = await db.get(User, row.student_id)
    assert owner is not None
    db.add(
        User(
            login="entry-other",
            password_hash=owner.password_hash,
            role="STUDENT",
            last_name="Другой",
            first_name="Обучающийся",
        )
    )
    await db.flush()
    await sign_in(client, "entry-other", "student")
    assert (await client.get(prefix)).status_code == 404
    assert (await client.post(prefix + "/draft", json=body, headers=headers)).status_code == 404
    assert (
        await client.post(f"/api/v1/teacher/assignments/{row.id}/reuse-card")
    ).status_code == 403


async def test_entry_teacher_finish_keeps_draft_and_scores_missing_submission(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    rows[0].task_mode = "CARD_ENTRY"
    lesson.settings = {**lesson.settings, "grammar_check_enabled": False}
    await db.flush()
    await tick(db, datetime.now(UTC) - timedelta(seconds=31))
    await tick(db, datetime.now(UTC))
    assert rows[0].state == "EXPIRED"
    await sign_in(client, "teacher", "teacher")
    finish = await client.post(f"/api/v1/teacher/lessons/{lesson.id}/finish")
    assert finish.status_code == 200, finish.text
    assert rows[0].score and rows[0].score["axes"]["completeness"]["score"] == 0
    codes = [item["code"] for item in rows[0].score["violations"]]
    assert "CARD_NOT_SUBMITTED" in codes and "PRIMARY_DEADLINE_EXCEEDED" in codes


async def test_entry_validation_queued_wrong_mode_unknown_identifiers_and_revision(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    _, rows = await lesson_fixture(db, 1)
    await sign_in(client)
    prefix = f"/api/v1/student/assignments/{rows[0].id}"
    body: dict[str, Any] = {"revision": 0, "card": {}}
    headers = {"Idempotency-Key": str(uuid4())}
    assert (await client.post(prefix + "/draft", json=body, headers=headers)).status_code == 404
    await tick(db, datetime.now(UTC))
    await client.get(prefix)
    assert (await client.post(prefix + "/draft", json=body, headers=headers)).status_code == 400
    rows[0].task_mode = "CARD_ENTRY"
    await db.flush()
    assert (
        await client.post(prefix + "/submit-card", json={"revision": 0}, headers=headers)
    ).status_code == 400
    for card in [
        {"incident_type_id": str(uuid4())},
        {"modifiers": ["BOGUS"]},
        {"modifiers": ["VICTIMS", "VICTIMS"]},
        {"notified_services": ["INFORMATION"]},
        {"notified_services": ["MCHS", "MCHS"]},
        {"reference": {"expected_status": "ACCEPTED"}},
    ]:
        body["card"] = card
        assert (await client.post(prefix + "/draft", json=body, headers=headers)).status_code == 400
    body["card"] = {}
    assert (await client.post(prefix + "/draft", json=body, headers=headers)).status_code == 200
    stale = await client.post(prefix + "/submit-card", json={"revision": 0}, headers=headers)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "DRAFT_CONFLICT"
    directory = (await client.get("/api/v1/student/entry-directory")).json()
    assert "INFORMATION" not in [row["code"] for row in directory["services"]]
    group = directory["groups"][0]["id"]
    types = (await client.get(f"/api/v1/student/entry-types?group_id={group}")).json()["items"]
    assert types
    entry_type = (await client.get(f"/api/v1/student/entry-types/{types[0]['id']}")).json()
    assert entry_type["group_id"] == group
    assert (
        await client.get(f"/api/v1/student/entry-types?group_id={group}&q=несуществующий")
    ).json()["items"] == []
    assert (
        await client.get(
            f"/api/v1/student/entry-services?incident_type_id={types[0]['id']}&modifiers=BOGUS"
        )
    ).status_code == 400


@pytest.mark.parametrize(
    "mode,task,expected",
    [
        ("CARD_ENTRY", None, 200),
        ("CARD_ENTRY", "CARD_ACTIONS", 400),
        ("CARD_ACTIONS", "CARD_ENTRY", 400),
        ("MIXED", "CARD_ENTRY", 200),
        ("MIXED", "CARD_ACTIONS", 200),
    ],
)
async def test_assignment_training_mode_matches_lesson(
    client: AsyncClient, db: AsyncSession, mode: str, task: str | None, expected: int
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    lesson.settings = {**lesson.settings, "training_mode": mode}
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    item: dict[str, Any] = {
        "student_id": str(rows[0].student_id),
        "scenario_id": str(rows[0].scenario_id),
    }
    if task:
        item["task_mode"] = task
    result = await client.post(
        f"/api/v1/teacher/lessons/{lesson.id}/assign", json={"assignments": [item]}
    )
    assert result.status_code == expected, result.text
