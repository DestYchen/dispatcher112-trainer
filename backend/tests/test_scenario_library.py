from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.generation import GenerationInput
from app.db.models import Scenario, Street
from app.generation.builder import build_scenario
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_copy_edit_archive_delete_preserves_assigned_source(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    lesson, assignments = await lesson_fixture(db, 1)
    source = await db.get(Scenario, assignments[0].scenario_id)
    assert source
    original = dict(source.card_payload)
    await sign_in(client, "teacher", "teacher")
    copied = await client.post(f"/api/v1/teacher/scenarios/{source.id}/copy")
    assert copied.status_code == 201, copied.text
    identifier = copied.json()["id"]
    candidate = await db.get(Scenario, UUID(identifier))
    assert (
        candidate
        and candidate.status == "PENDING_REVIEW"
        and candidate.author_id == lesson.teacher_id
    )
    assert "lesson_id" not in candidate.card_payload
    assert source.card_payload == original
    assert (
        await client.post(
            f"/api/v1/teacher/scenarios/{identifier}/approve",
            json={"difficulty": 3, "title": "Исправленный учебный сценарий"},
        )
    ).status_code == 200
    body = {
        "assignments": [{"student_id": str(assignments[0].student_id), "scenario_id": identifier}]
    }
    assert (
        await client.post(f"/api/v1/teacher/lessons/{lesson.id}/assign", json=body)
    ).status_code == 200
    assert (await client.delete(f"/api/v1/teacher/scenarios/{identifier}")).status_code == 409
    archive = await client.patch(
        f"/api/v1/teacher/scenarios/{identifier}/archive", json={"archived": True}
    )
    assert archive.status_code == 200 and candidate.card_payload["archived"]
    assert (
        await client.post(f"/api/v1/teacher/lessons/{lesson.id}/assign", json=body)
    ).status_code == 400
    listed = (await client.get("/api/v1/teacher/scenarios?status=APPROVED")).json()["items"]
    assert identifier not in {row["id"] for row in listed}
    listed = (
        await client.get("/api/v1/teacher/scenarios?status=APPROVED&include_archived=true")
    ).json()["items"]
    assert identifier in {row["id"] for row in listed}
    assert (
        await client.patch(
            f"/api/v1/teacher/scenarios/{source.id}/archive", json={"archived": True}
        )
    ).status_code == 404
    assert (await client.delete(f"/api/v1/teacher/scenarios/{source.id}")).status_code == 404
    independent = (await client.post(f"/api/v1/teacher/scenarios/{identifier}/copy")).json()["id"]
    independent_row = await db.get(Scenario, UUID(independent))
    assert independent_row and not independent_row.card_payload.get("archived")
    assert (await client.delete(f"/api/v1/teacher/scenarios/{independent}")).status_code == 204
    assert source.card_payload == original
    assert (await client.post(f"/api/v1/teacher/scenarios/{uuid4()}/copy")).status_code == 404


async def test_module_scenario_cannot_be_deleted(client: AsyncClient, db: AsyncSession) -> None:
    lesson, assignments = await lesson_fixture(db, 1)
    scenario = await db.get(Scenario, assignments[0].scenario_id)
    assert scenario
    scenario.author_id = lesson.teacher_id
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    identifier = (await client.post(f"/api/v1/teacher/scenarios/{scenario.id}/copy")).json()["id"]
    assert (
        await client.post(f"/api/v1/teacher/scenarios/{identifier}/approve", json={"difficulty": 3})
    ).status_code == 200
    created = await client.post(
        "/api/v1/teacher/modules",
        json={
            "title": "Практикум",
            "instructions": "Выполните на занятии",
            "difficulty": 3,
            "scenario_ids": [identifier],
        },
    )
    assert created.status_code == 201
    assert (await client.delete(f"/api/v1/teacher/scenarios/{identifier}")).status_code == 409


async def test_generation_respects_selected_location(db: AsyncSession) -> None:
    lesson, _ = await lesson_fixture(db, 0)
    street = (await db.scalars(select(Street).order_by(Street.name_norm))).first()
    assert street
    request = GenerationInput(
        count=1, generation_backend="template", street_ids=[street.id]
    ).model_dump(mode="json")
    result = await build_scenario(db, lesson.id, lesson.teacher_id, request, 12)
    assert result.card_payload["address"]["raw"].startswith(street.name + ",")
    assert result.card_payload["generation"]["backend"] == "template"
