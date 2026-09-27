from datetime import datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Assignment, AuditLog, LessonParticipant, Scenario, User
from tests.test_learning import sign_in
from tests.test_learning_model import synthetic_records
from tests.test_lessons import lesson_fixture


async def test_analytics_real_training_access_and_prospective_comparison(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    lesson, initial = await lesson_fixture(db, 1)
    original = (await db.scalars(select(User).where(User.login == "student1"))).one()
    template = await db.get(Scenario, initial[0].scenario_id)
    assert template
    scenario_ids = {}
    for difficulty in range(1, 11):
        scenario = Scenario(
            title=f"Сложность {difficulty}",
            source="MANUAL",
            status="APPROVED",
            origin=template.origin,
            incident_type_id=template.incident_type_id,
            difficulty=difficulty,
            card_payload=template.card_payload,
            reference=template.reference,
            author_id=lesson.teacher_id,
        )
        db.add(scenario)
        await db.flush()
        scenario_ids[difficulty] = scenario.id
    students = {"0": original.id}
    for index in range(1, 20):
        participant = User(
            id=uuid4(),
            login=f"analytics-{index}",
            password_hash=original.password_hash,
            role="STUDENT",
            first_name="Учебный",
            last_name=str(index),
            service_id=original.service_id,
        )
        db.add(participant)
        await db.flush()
        students[str(index)] = participant.id
        db.add(LessonParticipant(lesson_id=lesson.id, student_id=participant.id))
    for record in synthetic_records():
        at = datetime.fromisoformat(record["closed_at"])
        db.add(
            Assignment(
                lesson_id=lesson.id,
                student_id=students[record["student_id"]],
                scenario_id=scenario_ids[record["difficulty"]],
                card_number=record["id"],
                task_mode=record["task_mode"],
                state="CLOSED",
                delivered_at=at - timedelta(seconds=30),
                closed_at=at,
                score={
                    "total": 75,
                    "axes": {axis: {"score": score} for axis, score in record["axes"].items()},
                    "violations": [],
                },
            )
        )
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    body = {
        "dataset_kind": "SYNTHETIC",
        "dataset_note": "Искусственные траектории для проверки алгоритма, не реальные учащиеся.",
    }
    assert (await client.post("/api/v1/teacher/analytics/models", json=body)).status_code == 409
    lesson.status = "FINISHED"
    await db.flush()
    trained = await client.post("/api/v1/teacher/analytics/models", json=body)
    assert trained.status_code == 201, trained.text
    model = trained.json()
    assert model["training_count"] == 192 and model["test_count"] == 48
    assert model["beats_baseline"] and "layers" not in model
    repeat = await client.post("/api/v1/teacher/analytics/models", json=body)
    assert repeat.json()["version"] == model["version"]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "LEARNING_MODEL_TRAINED")
        )
        == 1
    )
    request = {"student_id": str(original.id), "difficulty": 3, "task_mode": "CARD_ACTIONS"}
    forecast = await client.post("/api/v1/teacher/analytics/forecast", json=request)
    assert forecast.status_code == 201, forecast.text
    first = forecast.json()
    assert first["dataset_kind"] == "SYNTHETIC"
    group = (
        await client.post(
            "/api/v1/teacher/groups", json={"title": "Разбор", "student_ids": [str(original.id)]}
        )
    ).json()
    analysis = await client.get(f"/api/v1/teacher/groups/{group['id']}/analytics")
    assert analysis.status_code == 200
    assert analysis.json()["cards_count"] == 15
    assert analysis.json()["forecast_students_count"] == 1
    assert analysis.json()["predicted_axes"] == first["predicted_axes"]
    assert (await client.get(f"/api/v1/teacher/groups/{uuid4()}/analytics")).status_code == 404
    assert (await client.post("/api/v1/teacher/analytics/forecast", json=request)).json() == first
    assert (
        await client.post(
            "/api/v1/teacher/analytics/forecast", json={**request, "student_id": str(uuid4())}
        )
    ).status_code == 404
    at = datetime.fromisoformat(first["created_at"])
    row = initial[0]
    row.scenario_id = scenario_ids[3]
    row.score = {
        "total": 80,
        "axes": {axis: {"score": 80} for axis in first["predicted_axes"]},
        "violations": [],
    }
    row.state, row.closed_at = "CLOSED", at + timedelta(seconds=90)
    row.delivered_at = at - timedelta(seconds=1)
    await db.flush()
    await sign_in(client, "student1", "student")
    history = (await client.get("/api/v1/learning/forecasts")).json()["items"]
    assert history[0]["actual_axes"] is None  # Already started card is not a future observation.
    row.delivered_at = at + timedelta(seconds=1)
    await db.flush()
    history = (await client.get("/api/v1/learning/forecasts")).json()["items"]
    assert history[0]["actual_assignment_id"] == str(row.id)
    assert history[0]["actual_axes"]["correctness"] == 80
    assert history[0]["absolute_error"]["correctness"] == round(
        abs(80 - first["predicted_axes"]["correctness"]), 2
    )
    assert (await client.get(f"/api/v1/learning/forecasts?student_id={uuid4()}")).status_code == 404
    assert (await client.post("/api/v1/teacher/analytics/models", json=body)).status_code == 403
    await sign_in(client, "admin", "admin")
    assert (await client.get("/api/v1/teacher/analytics/models")).status_code == 403
    assert (await client.get("/api/v1/learning/forecasts")).status_code == 403


async def test_analytics_insufficient_data_is_explicit(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    lesson.status = "FINISHED"
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    result = await client.post(
        "/api/v1/teacher/analytics/models",
        json={
            "dataset_kind": "SYNTHETIC",
            "dataset_note": "Данных пока недостаточно для обучения.",
        },
    )
    assert result.status_code == 400 and result.json()["error"]["code"] == "INSUFFICIENT_DATA"
    result = await client.post(
        "/api/v1/teacher/analytics/forecast",
        json={"student_id": str(rows[0].student_id), "difficulty": 3, "task_mode": "CARD_ACTIONS"},
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "MODEL_UNAVAILABLE"
    result = await client.get("/api/v1/teacher/analytics/models")
    assert result.json()["items"] == []
