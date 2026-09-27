import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pypdf import PdfWriter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, LearningMaterial, User
from app.domain.materials import DOCX_TYPE, MAX_FILE_SIZE, validate_document
from tests.test_auth import sign_in as fresh_sign_in
from tests.test_lessons import lesson_fixture


async def sign_in(client: AsyncClient, login: str, password: str) -> None:
    client.cookies.clear()
    await fresh_sign_in(client, login, password)


async def add_other_student(db: AsyncSession) -> None:
    original = (await db.scalars(select(User).where(User.login == "student1"))).one()
    db.add(
        User(
            login="student2",
            password_hash=original.password_hash,
            role="STUDENT",
            first_name="Второй",
            last_name="Учащийся",
            service_id=original.service_id,
        )
    )
    await db.flush()


def docx(extra: dict[str, str] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": "<document/>",
            **(extra or {}),
        }.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_document_formats() -> None:
    assert validate_document("инструкция.docx", docx()) == DOCX_TYPE
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    output = io.BytesIO()
    writer.write(output)
    assert validate_document("инструкция.pdf", output.getvalue()) == "application/pdf"


@pytest.mark.parametrize(
    "filename,content",
    [
        ("file.exe", b"data"),
        ("file.pdf", b"invalid"),
        ("file.pdf", b"%PDF-1.7"),
        ("file.pdf", b"%PDF-1.7\n%%EOF"),
        ("file.docx", b"invalid"),
        ("file.docx", docx({"../file.xml": "<x/>"})),
        ("file.docx", docx({"word/vbaProject.bin": "macro"})),
        (
            "file.docx",
            docx(
                {
                    "word/_rels/document.xml.rels": "<Relationships><Relationship "
                    'TargetMode="External"/></Relationships>'
                }
            ),
        ),
        ("file.docx", docx({"word/document.xml": '<!DOCTYPE x [<!ENTITY x "data">]><x/>'})),
        ("file.pdf", b"x" * (MAX_FILE_SIZE + 1)),
        ("file.pdf", b""),
    ],
)
def test_document_rejects_invalid_or_external_content(filename: str, content: bytes) -> None:
    with pytest.raises(ValueError):
        validate_document(filename, content)


async def test_material_group_module_permissions_progress_and_audit(
    client: AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("app.api.learning.MATERIAL_ROOT", tmp_path)
    await add_other_student(db)
    lesson, assignments = await lesson_fixture(db, 1)
    student_id = assignments[0].student_id
    await sign_in(client, "teacher", "teacher")
    uploaded = await client.post(
        "/api/v1/teacher/materials",
        data={"title": "Адреса", "body": "Проверьте улицу"},
        files={"file": ("памятка.docx", docx(), DOCX_TYPE)},
    )
    assert uploaded.status_code == 201, uploaded.text
    material = uploaded.json()
    assert material["sha256"] and material["filename"] == "памятка.docx"
    assert (await client.get(material["download_url"])).content == docx()
    duplicate = await client.post(
        "/api/v1/teacher/groups", json={"title": "Группа", "student_ids": [str(student_id)] * 2}
    )
    assert duplicate.status_code == 400
    group = (
        await client.post(
            "/api/v1/teacher/groups", json={"title": "Группа", "student_ids": [str(student_id)]}
        )
    ).json()
    module = await client.post(
        "/api/v1/teacher/modules",
        json={
            "title": "Адрес",
            "instructions": "Изучите памятку",
            "difficulty": 2,
            "material_ids": [material["id"]],
            "scenario_ids": [str(assignments[0].scenario_id)],
        },
    )
    assert module.status_code == 201, module.text
    module_id = module.json()["id"]
    assign_url = f"/api/v1/teacher/modules/{module_id}/groups/{group['id']}"
    assert (await client.post(assign_url)).status_code == 200
    assert (await client.post(assign_url)).status_code == 200
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "MODULE_ASSIGNED")
        )
        == 1
    )
    await sign_in(client, "student1", "student")
    modules = (await client.get("/api/v1/student/modules")).json()["items"]
    assert len(modules) == 1 and modules[0]["materials"][0]["id"] == material["id"]
    assert "scenario_ids" not in modules[0] and "СЕКРЕТ ЭТАЛОНА" not in str(modules)
    response = await client.get(material["download_url"])
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    first = await client.post(f"/api/v1/student/modules/{module_id}/complete")
    repeat = await client.post(f"/api/v1/student/modules/{module_id}/complete")
    assert first.json() == repeat.json() and first.status_code == 200
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "MODULE_READ")
        )
        == 1
    )
    assert (await client.post("/api/v1/teacher/groups", json={})).status_code == 403
    await sign_in(client, "student2", "student")
    assert not (await client.get("/api/v1/student/modules")).json()["items"]
    assert (await client.get(material["download_url"])).status_code == 404
    assert (await client.post(f"/api/v1/student/modules/{module_id}/complete")).status_code == 404
    await sign_in(client, "admin", "admin")
    assert (await client.get("/api/v1/teacher/materials")).status_code == 403
    assert (await client.get(material["download_url"])).status_code == 403
    await sign_in(client, "teacher", "teacher")
    second_id = await db.scalar(select(User.id).where(User.login == "student2"))
    assert (
        await client.patch(
            f"/api/v1/teacher/groups/{group['id']}",
            json={"title": "Новый состав", "student_ids": [str(second_id)]},
        )
    ).status_code == 200
    await sign_in(client, "student1", "student")
    assert (await client.get(material["download_url"])).status_code == 404


async def test_material_validation_archive_and_foreign_ownership(
    client: AsyncClient, db: AsyncSession
) -> None:
    await sign_in(client, "teacher", "teacher")
    for values in ({"title": " ", "body": "Текст"}, {"title": "Материал"}):
        assert (await client.post("/api/v1/teacher/materials", data=values)).status_code == 400
    assert (
        await client.post(
            "/api/v1/teacher/materials",
            data={"title": "Материал"},
            files={"file": ("file.pdf", b"invalid")},
        )
    ).status_code == 400
    created = (
        await client.post(
            "/api/v1/teacher/materials", data={"title": "Материал", "body": "Памятка"}
        )
    ).json()
    material_id = created["id"]
    assert (
        await client.patch(f"/api/v1/teacher/materials/{material_id}", json={"archived": True})
    ).status_code == 200
    body = {
        "title": "Модуль",
        "instructions": "Инструкция",
        "difficulty": 1,
        "material_ids": [material_id],
    }
    assert (await client.post("/api/v1/teacher/modules", json=body)).status_code == 400
    assert (
        await client.patch(f"/api/v1/teacher/materials/{material_id}", json={"archived": False})
    ).status_code == 200
    assert (await client.post("/api/v1/teacher/modules", json=body)).status_code == 201
    row = await db.get(LearningMaterial, UUID(material_id))
    assert row
    row.teacher_id = (await db.scalars(select(User.id).where(User.login == "admin"))).one()
    await db.flush()
    assert (
        await client.patch(f"/api/v1/teacher/materials/{material_id}", json={"archived": True})
    ).status_code == 404
    assert (await client.post("/api/v1/teacher/modules", json=body)).status_code == 400
    assert (
        await client.patch(
            f"/api/v1/teacher/groups/{uuid4()}", json={"title": "X", "student_ids": [str(uuid4())]}
        )
    ).status_code == 404


async def test_history_feedback_scope_original_score_and_pagination(
    client: AsyncClient, db: AsyncSession
) -> None:
    await add_other_student(db)
    lesson, assignments = await lesson_fixture(db, 3)
    await sign_in(client, "teacher", "teacher")
    base = f"/api/v1/teacher/assignments/{assignments[0].id}/feedback"
    assert (await client.post(base, json={"body": "Проверьте адрес"})).status_code == 409
    for row in assignments:
        row.score = {"total": 70, "axes": {}, "violations": []}
        row.state, row.closed_at = "CLOSED", datetime.now(UTC)
    assignments[0].teacher_override = {"total": 75, "comment": "Уточнение"}
    await db.flush()
    assert (await client.post(base, json={"body": "Проверьте адрес"})).status_code == 201
    assert assignments[0].score == {"total": 70, "axes": {}, "violations": []}
    students = (await client.get("/api/v1/teacher/progress/students")).json()["items"]
    assert any(item["id"] == str(assignments[0].student_id) for item in students)
    await sign_in(client, "student1", "student")
    first = (await client.get("/api/v1/learning/history?limit=2")).json()
    second = (
        await client.get(f"/api/v1/learning/history?limit=2&cursor={first['next_cursor']}")
    ).json()
    items = first["items"] + second["items"]
    assert len(items) == 3 and len({row["assignment_id"] for row in items}) == 3
    assert sum(len(row["feedback"]) for row in items) == 1
    assert (await client.get(f"/api/v1/learning/history?student_id={uuid4()}")).status_code == 404
    await sign_in(client, "student2", "student")
    assert not (await client.get("/api/v1/learning/history")).json()["items"]
    await sign_in(client, "teacher", "teacher")
    assert (await client.get(f"/api/v1/learning/history?student_id={uuid4()}")).status_code == 404
    # Move the lesson to another teacher's scope; knowledge of the UUID grants no access.
    lesson.teacher_id = (await db.scalars(select(User.id).where(User.login == "admin"))).one()
    await db.flush()
    assert (await client.post(base, json={"body": "Проверьте адрес"})).status_code == 404
    assert (
        await client.get(f"/api/v1/learning/history?student_id={assignments[0].student_id}")
    ).status_code == 404
