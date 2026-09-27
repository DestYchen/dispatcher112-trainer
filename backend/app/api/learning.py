import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4
from xml.etree.ElementTree import Element, tostring

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, Student, TrainingUser, require_role
from app.api.errors import APIError
from app.db.models import (
    Assignment,
    AssignmentFeedback,
    AuditLog,
    GroupMember,
    LearningGroup,
    LearningMaterial,
    LearningModule,
    Lesson,
    ModuleAssignment,
    ModuleProgress,
    Scenario,
    User,
)
from app.domain.exports import xml_value
from app.domain.materials import MAX_FILE_SIZE, validate_document

router = APIRouter(tags=["learning"])
TeacherOnly = Annotated[User, Depends(require_role("TEACHER"))]
MATERIAL_ROOT = Path("/data/materials")


def audit(
    db: AsyncSession,
    user: User,
    action: str,
    kind: str,
    identifier: UUID,
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id, action=action, entity_type=kind, entity_id=identifier, payload=payload
        )
    )


class GroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    student_ids: list[UUID] = Field(min_length=1, max_length=100)


async def validate_students(db: AsyncSession, ids: list[UUID]) -> None:
    found = list(
        await db.scalars(
            select(User.id).where(
                User.id.in_(ids), User.role == "STUDENT", User.is_active.is_(True)
            )
        )
    )
    if len(found) != len(ids):
        raise APIError(400, "VALIDATION_ERROR", "Выберите неповторяющихся действующих учащихся.")


@router.get("/teacher/groups")
async def groups(db: DB, user: TeacherOnly) -> dict[str, Any]:
    rows = list(
        await db.scalars(
            select(LearningGroup)
            .where(LearningGroup.teacher_id == user.id)
            .order_by(LearningGroup.created_at.desc())
        )
    )
    members = list(
        (
            await db.execute(
                select(GroupMember.group_id, User.id, User.last_name, User.first_name)
                .join(User, User.id == GroupMember.student_id)
                .where(GroupMember.group_id.in_([row.id for row in rows]))
            )
        ).all()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "title": row.title,
                "students": [
                    {"id": str(student_id), "name": f"{last} {first}"}
                    for group_id, student_id, last, first in members
                    if group_id == row.id
                ],
            }
            for row in rows
        ]
    }


@router.post("/teacher/groups", status_code=201)
async def create_group(body: GroupInput, db: DB, user: TeacherOnly) -> dict[str, str]:
    await validate_students(db, body.student_ids)
    row = LearningGroup(id=uuid4(), teacher_id=user.id, title=body.title)
    db.add(row)
    await db.flush()
    db.add_all([GroupMember(group_id=row.id, student_id=value) for value in body.student_ids])
    audit(db, user, "GROUP_CREATED", "learning_group", row.id, body.model_dump(mode="json"))
    await db.commit()
    return {"id": str(row.id)}


@router.patch("/teacher/groups/{group_id}")
async def edit_group(group_id: UUID, body: GroupInput, db: DB, user: TeacherOnly) -> dict[str, str]:
    row = await db.scalar(
        select(LearningGroup)
        .where(LearningGroup.id == group_id, LearningGroup.teacher_id == user.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Группа не найдена.")
    await validate_students(db, body.student_ids)
    row.title = body.title
    await db.execute(delete(GroupMember).where(GroupMember.group_id == group_id))
    db.add_all([GroupMember(group_id=group_id, student_id=value) for value in body.student_ids])
    audit(db, user, "GROUP_UPDATED", "learning_group", group_id, body.model_dump(mode="json"))
    await db.commit()
    return {"id": str(group_id)}


def material_payload(row: LearningMaterial) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "body": row.body,
        "difficulty": row.difficulty,
        "filename": row.filename,
        "sha256": row.sha256,
        "archived": row.archived,
        "download_url": f"/api/v1/learning/materials/{row.id}/file" if row.filename else None,
    }


@router.get("/teacher/materials")
async def materials(db: DB, user: TeacherOnly) -> dict[str, Any]:
    rows = await db.scalars(
        select(LearningMaterial)
        .where(LearningMaterial.teacher_id == user.id)
        .order_by(LearningMaterial.created_at.desc())
    )
    return {"items": [material_payload(row) for row in rows]}


@router.get("/teacher/materials/{material_id}/export.xml")
async def material_xml(material_id: UUID, db: DB, user: TeacherOnly) -> Response:
    row = await db.get(LearningMaterial, material_id)
    if row is None or row.teacher_id != user.id:
        raise APIError(404, "NOT_FOUND", "Материал не найден.")
    root = Element("learning_material", {"schema": "1"})
    xml_value(root, material_payload(row))
    audit(db, user, "LEARNING_MATERIAL_EXPORTED", "learning_material", row.id, {"format": "XML"})
    await db.commit()
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="material-{material_id}.xml"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/teacher/materials", status_code=201)
async def create_material(
    db: DB,
    user: TeacherOnly,
    title: Annotated[str, Form(min_length=1, max_length=255)],
    body: Annotated[str, Form(max_length=30000)] = "",
    difficulty: Annotated[int, Form(ge=1, le=10)] = 1,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    if not title.strip() or (not body.strip() and file is None):
        raise APIError(400, "VALIDATION_ERROR", "Укажите название и текст либо документ.")
    row = LearningMaterial(
        id=uuid4(),
        teacher_id=user.id,
        title=title.strip(),
        body=body.strip(),
        difficulty=difficulty,
        archived=False,
    )
    if file:
        content = await file.read(MAX_FILE_SIZE + 1)
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or len(filename) > 255:
            raise APIError(400, "VALIDATION_ERROR", "Некорректное имя документа.")
        try:
            row.content_type = await asyncio.to_thread(validate_document, filename, content)
        except ValueError as error:
            raise APIError(400, "VALIDATION_ERROR", str(error)) from error
        row.filename, row.sha256 = filename, hashlib.sha256(content).hexdigest()
        await asyncio.to_thread(MATERIAL_ROOT.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((MATERIAL_ROOT / str(row.id)).write_bytes, content)
    db.add(row)
    audit(
        db,
        user,
        "MATERIAL_CREATED",
        "learning_material",
        row.id,
        {"title": row.title, "sha256": row.sha256},
    )
    await db.commit()
    return material_payload(row)


class ArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


@router.patch("/teacher/materials/{material_id}")
async def archive_material(
    material_id: UUID, body: ArchiveInput, db: DB, user: TeacherOnly
) -> dict[str, Any]:
    row = await db.scalar(
        select(LearningMaterial)
        .where(LearningMaterial.id == material_id, LearningMaterial.teacher_id == user.id)
        .with_for_update()
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Материал не найден.")
    row.archived = body.archived
    audit(db, user, "MATERIAL_ARCHIVED", "learning_material", row.id, body.model_dump())
    await db.commit()
    return material_payload(row)


class ModuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    instructions: str = Field(min_length=1, max_length=10000)
    difficulty: int = Field(ge=1, le=10)
    material_ids: list[UUID] = Field(default_factory=list, max_length=100)
    scenario_ids: list[UUID] = Field(default_factory=list, max_length=100)


@router.post("/teacher/modules", status_code=201)
async def create_module(body: ModuleInput, db: DB, user: TeacherOnly) -> dict[str, str]:
    materials = list(
        await db.scalars(
            select(LearningMaterial.id).where(
                LearningMaterial.id.in_(body.material_ids),
                LearningMaterial.teacher_id == user.id,
                LearningMaterial.archived.is_(False),
            )
        )
    )
    scenarios = list(
        await db.scalars(
            select(Scenario.id)
            .where(
                Scenario.id.in_(body.scenario_ids),
                Scenario.status == "APPROVED",
                Scenario.card_payload["archived"].as_boolean().is_not(True),
            )
            .with_for_update(read=True)
        )
    )
    if len(materials) != len(body.material_ids) or len(scenarios) != len(body.scenario_ids):
        raise APIError(
            400, "VALIDATION_ERROR", "Выберите доступные материалы и утверждённые сценарии."
        )
    row = LearningModule(id=uuid4(), teacher_id=user.id, **body.model_dump(mode="json"))
    db.add(row)
    audit(db, user, "MODULE_CREATED", "learning_module", row.id, body.model_dump(mode="json"))
    await db.commit()
    return {"id": str(row.id)}


@router.get("/teacher/modules")
async def teacher_modules(db: DB, user: TeacherOnly) -> dict[str, Any]:
    rows = list(
        await db.scalars(
            select(LearningModule)
            .where(LearningModule.teacher_id == user.id)
            .order_by(LearningModule.created_at.desc())
        )
    )
    assignments = list(
        (
            await db.execute(
                select(ModuleAssignment).where(
                    ModuleAssignment.module_id.in_([row.id for row in rows])
                )
            )
        ).scalars()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "title": row.title,
                "instructions": row.instructions,
                "difficulty": row.difficulty,
                "material_ids": row.material_ids,
                "scenario_ids": row.scenario_ids,
                "archived": row.archived,
                "group_ids": [
                    str(item.group_id) for item in assignments if item.module_id == row.id
                ],
            }
            for row in rows
        ]
    }


@router.post("/teacher/modules/{module_id}/groups/{group_id}")
async def assign_module(
    module_id: UUID, group_id: UUID, db: DB, user: TeacherOnly
) -> dict[str, str]:
    group = await db.scalar(
        select(LearningGroup)
        .where(LearningGroup.id == group_id, LearningGroup.teacher_id == user.id)
        .with_for_update()
    )
    module = await db.scalar(
        select(LearningModule)
        .where(
            LearningModule.id == module_id,
            LearningModule.teacher_id == user.id,
            LearningModule.archived.is_(False),
        )
        .with_for_update()
    )
    if not group or not module:
        raise APIError(404, "NOT_FOUND", "Группа или модуль не найдены.")
    if await db.get(ModuleAssignment, (module_id, group_id)) is None:
        db.add(ModuleAssignment(module_id=module_id, group_id=group_id))
        audit(
            db, user, "MODULE_ASSIGNED", "learning_module", module_id, {"group_id": str(group_id)}
        )
        await db.commit()
    return {"module_id": str(module_id), "group_id": str(group_id)}


async def available_modules(db: AsyncSession, user: User) -> list[LearningModule]:
    return list(
        await db.scalars(
            select(LearningModule)
            .join(ModuleAssignment)
            .join(GroupMember, GroupMember.group_id == ModuleAssignment.group_id)
            .where(GroupMember.student_id == user.id)
            .distinct()
            .order_by(LearningModule.created_at.desc())
        )
    )


@router.get("/student/modules")
async def student_modules(db: DB, user: Student) -> dict[str, Any]:
    modules = await available_modules(db, user)
    material_ids = {UUID(value) for row in modules for value in row.material_ids}
    materials = list(
        await db.scalars(select(LearningMaterial).where(LearningMaterial.id.in_(material_ids)))
    )
    progress = list(
        await db.scalars(select(ModuleProgress).where(ModuleProgress.student_id == user.id))
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "title": row.title,
                "instructions": row.instructions,
                "difficulty": row.difficulty,
                "materials": [
                    material_payload(item) for item in materials if str(item.id) in row.material_ids
                ],
                "scenarios_count": len(row.scenario_ids),
                "completed_at": next(
                    (item.completed_at for item in progress if item.module_id == row.id), None
                ),
            }
            for row in modules
        ]
    }


@router.post("/student/modules/{module_id}/complete")
async def complete_module(module_id: UUID, db: DB, user: Student) -> dict[str, Any]:
    if module_id not in {row.id for row in await available_modules(db, user)}:
        raise APIError(404, "NOT_FOUND", "Модуль не назначен.")
    # Serialize duplicate submissions for the same learner, including two browser tabs.
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = await db.get(ModuleProgress, (module_id, user.id))
    if row is None:
        row = ModuleProgress(module_id=module_id, student_id=user.id)
        db.add(row)
    if row.completed_at is None:
        row.completed_at = datetime.now(UTC)
        audit(db, user, "MODULE_READ", "learning_module", module_id)
        await db.commit()
    return {"completed_at": row.completed_at}


@router.get("/learning/materials/{material_id}/file")
async def download_material(material_id: UUID, db: DB, user: TrainingUser) -> FileResponse:
    row = await db.get(LearningMaterial, material_id)
    allowed = bool(row and row.teacher_id == user.id and user.role == "TEACHER")
    if user.role == "STUDENT":
        allowed = any(
            str(material_id) in module.material_ids for module in await available_modules(db, user)
        )
    if not row or not allowed or not row.filename:
        raise APIError(404, "NOT_FOUND", "Документ не найден.")
    path = MATERIAL_ROOT / str(row.id)
    if not await asyncio.to_thread(path.is_file):
        raise APIError(503, "MATERIAL_UNAVAILABLE", "Файл временно недоступен. Повторите позже.")
    return FileResponse(
        path,
        media_type=row.content_type,
        filename=row.filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body: str = Field(min_length=5, max_length=5000)


@router.post("/teacher/assignments/{assignment_id}/feedback", status_code=201)
async def feedback(
    assignment_id: UUID, body: FeedbackInput, db: DB, user: TeacherOnly
) -> dict[str, str]:
    assignment = await db.scalar(
        select(Assignment)
        .join(Lesson)
        .where(Assignment.id == assignment_id, Lesson.teacher_id == user.id)
        .with_for_update(of=Assignment)
    )
    if assignment is None:
        raise APIError(404, "NOT_FOUND", "Задание не найдено.")
    if assignment.score is None:
        raise APIError(409, "INVALID_TRANSITION", "Обратная связь доступна после оценки задания.")
    row = AssignmentFeedback(
        id=uuid4(), assignment_id=assignment_id, teacher_id=user.id, body=body.body
    )
    db.add(row)
    audit(
        db,
        user,
        "FEEDBACK_ADDED",
        "assignment",
        assignment_id,
        {"feedback_id": str(row.id), "body": body.body},
    )
    await db.commit()
    return {"id": str(row.id)}
