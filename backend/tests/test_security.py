from typing import Any
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LessonParticipant, User
from app.main import app
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture

protected = [
    (method, route.path)
    for route in app.routes
    if isinstance(route, APIRoute) and route.path.startswith(("/api/v1/teacher/", "/api/v1/admin/"))
    for method in route.methods
    if method in {"GET", "POST", "PATCH", "DELETE"}
]


@pytest.mark.parametrize(("method", "path"), protected)
async def test_every_teacher_admin_endpoint_rejects_students(
    client: AsyncClient, method: str, path: str
) -> None:
    await sign_in(client)
    for parameter in (
        "lesson_id",
        "student_id",
        "assignment_id",
        "scenario_id",
        "job_id",
        "user_id",
    ):
        path = path.replace("{" + parameter + "}", str(uuid4()))
    response = await client.request(method, path, json={} if method != "GET" else None)
    assert response.status_code == 403, (method, path, response.text)


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("POST", "/status", {"status": "ACCEPTED"}),
        ("POST", "/check-text", {"text": "Дубнинская", "field": "address"}),
        ("POST", "/call", {"number": "2201"}),
        ("POST", "/report", {"call_id": str(uuid4()), "transcript": "Доклад", "duration_ms": 1}),
    ],
)
async def test_all_card_routes_hide_foreign_assignment(
    client: AsyncClient, db: AsyncSession, method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    rows[0].student_id = lesson.teacher_id
    rows[0].state = "DELIVERED"
    await db.flush()
    await sign_in(client)
    response = await client.request(
        method,
        f"/api/v1/student/assignments/{rows[0].id}{suffix}",
        json=body,
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 404
    assert str(rows[0].id) not in (await client.get("/api/v1/student/state")).text


async def test_results_of_foreign_lesson_and_forged_role_are_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, rows = await lesson_fixture(db, 1)
    lesson.status = "FINISHED"
    member = (
        await db.scalars(select(LessonParticipant).where(LessonParticipant.lesson_id == lesson.id))
    ).one()
    member.student_id = lesson.teacher_id
    rows[0].student_id = lesson.teacher_id
    await db.flush()
    await sign_in(client)
    assert (await client.get(f"/api/v1/student/results?lesson_id={lesson.id}")).status_code == 404
    assert (
        await client.get(
            "/api/v1/teacher/lessons",
            headers={"X-Role": "ADMIN", "X-User-Id": str(lesson.teacher_id)},
        )
    ).status_code == 403
    user = await db.get(User, rows[0].student_id)
    assert user and user.role == "TEACHER"


async def test_metrics_use_route_templates_without_personal_data(client: AsyncClient) -> None:
    await sign_in(client)
    foreign = str(uuid4())
    await client.get(f"/api/v1/student/assignments/{foreign}")
    response = await client.get("/metrics")
    assert response.status_code == 200 and "text/plain" in response.headers["content-type"]
    assert "dispatcher_http_requests_total" in response.text
    assert "/api/v1/student/assignments/{assignment_id}" in response.text
    assert foreign not in response.text and "student1" not in response.text
    assert 'le="0.3"' in response.text and 'le="+Inf"' in response.text
