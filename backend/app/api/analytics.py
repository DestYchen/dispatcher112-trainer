import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, TrainingUser
from app.api.errors import APIError
from app.api.history import learning_student
from app.api.learning import TeacherOnly, audit
from app.db.models import (
    Assignment,
    GroupMember,
    LearningForecast,
    LearningGroup,
    LearningModel,
    Lesson,
    Scenario,
    User,
)
from app.domain.learning_model import AXES, digest, features, predict, train

router = APIRouter(tags=["analytics"])


@router.get("/teacher/groups/{group_id}/analytics")
async def group_analytics(group_id: UUID, db: DB, user: TeacherOnly) -> dict[str, Any]:
    group = await db.scalar(
        select(LearningGroup).where(
            LearningGroup.id == group_id, LearningGroup.teacher_id == user.id
        )
    )
    if group is None:
        raise APIError(404, "NOT_FOUND", "Группа не найдена.")
    members = list(
        await db.scalars(select(GroupMember.student_id).where(GroupMember.group_id == group_id))
    )
    scores = list(
        await db.scalars(
            select(Assignment.score)
            .join(Lesson)
            .where(
                Lesson.teacher_id == user.id,
                Assignment.student_id.in_(members),
                Assignment.score.is_not(None),
            )
        )
    )
    available = [score for score in scores if score]
    counts = Counter(str(item["code"]) for score in available for item in score["violations"])
    hints = {
        str(item["code"]): item.get("hint", "")
        for score in available
        for item in score["violations"]
    }
    latest: dict[UUID, LearningForecast] = {}
    for row in await db.scalars(
        select(LearningForecast)
        .where(LearningForecast.teacher_id == user.id, LearningForecast.student_id.in_(members))
        .order_by(LearningForecast.created_at.desc(), LearningForecast.id)
    ):
        latest.setdefault(row.student_id, row)
    actual = {}
    for axis in AXES:
        values = [score.get("axes", {}).get(axis, {}).get("score") for score in available]
        numeric = [float(value) for value in values if value is not None]
        actual[axis] = round(sum(numeric) / len(numeric), 2) if numeric else None
    predicted = (
        {
            axis: round(
                sum(row.payload["predicted_axes"][axis] for row in latest.values()) / len(latest), 2
            )
            for axis in AXES
        }
        if latest
        else None
    )
    return {
        "title": group.title,
        "students_count": len(members),
        "cards_count": len(available),
        "actual_axes": actual,
        "predicted_axes": predicted,
        "forecast_students_count": len(latest),
        "synthetic": any(row.payload["dataset_kind"] == "SYNTHETIC" for row in latest.values()),
        "violations": [
            {"code": code, "count": count, "hint": hints[code]}
            for code, count in counts.most_common(10)
        ],
    }


async def records(
    db: AsyncSession, teacher_id: UUID, student_id: UUID | None = None
) -> list[dict[str, Any]]:
    query = (
        select(Assignment, Scenario.difficulty)
        .join(Lesson)
        .join(Scenario, Scenario.id == Assignment.scenario_id)
        .where(Lesson.teacher_id == teacher_id, Assignment.score.is_not(None))
    )
    if student_id:
        query = query.where(Assignment.student_id == student_id)
    rows = list(
        (await db.execute(query.order_by(Assignment.closed_at, Assignment.id).limit(20001))).all()
    )
    if len(rows) > 20000:
        raise APIError(
            400, "VALIDATION_ERROR", "Выборка превышает 20 000 заданий; разделите учебные потоки."
        )
    result = []
    for row, difficulty in rows:
        assert row.score and row.closed_at
        axes = {axis: row.score.get("axes", {}).get(axis, {}).get("score") for axis in AXES}
        if any(value is None for value in axes.values()):
            continue
        result.append(
            {
                "id": str(row.id),
                "student_id": str(row.student_id),
                "closed_at": row.closed_at.isoformat(),
                "difficulty": difficulty,
                "task_mode": row.task_mode,
                "axes": axes,
            }
        )
    return result


def model_payload(row: LearningModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "version": row.version,
        "created_at": row.created_at,
        "dataset_kind": row.dataset_kind,
        "dataset_note": row.dataset_note,
        **{
            key: row.artifact[key]
            for key in (
                "schema",
                "algorithm",
                "sklearn",
                "seed",
                "dataset_sha256",
                "training_count",
                "test_count",
                "converged",
                "trained_through",
                "mae",
                "baseline_mae",
                "mae_overall",
                "baseline_mae_overall",
                "beats_baseline",
                "comparisons",
            )
        },
    }


@router.get("/teacher/analytics/models")
async def models(db: DB, user: TeacherOnly) -> dict[str, Any]:
    rows = await db.scalars(
        select(LearningModel)
        .where(LearningModel.teacher_id == user.id)
        .order_by(LearningModel.created_at.desc(), LearningModel.id)
        .limit(100)
    )
    return {"items": [model_payload(row) for row in rows]}


class TrainInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    dataset_kind: Literal["SYNTHETIC", "OBSERVED"]
    dataset_note: str = Field(min_length=15, max_length=2000)


@router.post("/teacher/analytics/models", status_code=201)
async def train_model(body: TrainInput, db: DB, user: TeacherOnly) -> dict[str, Any]:
    # A separate teacher-row lock prevents concurrent model fits from exhausting CPU/RAM.
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    active_lesson = await db.scalar(
        select(Lesson.id).where(Lesson.teacher_id == user.id, Lesson.status == "RUNNING").limit(1)
    )
    if active_lesson:
        raise APIError(
            409,
            "LESSON_RUNNING",
            "Подготовьте модель между занятиями, чтобы сохранить ресурсы голосовых сессий.",
        )
    source = await records(db, user.id)
    try:
        artifact = await asyncio.to_thread(train, source)
    except ValueError as error:
        raise APIError(400, "INSUFFICIENT_DATA", str(error)) from error
    version = digest({"artifact": artifact["version"], **body.model_dump()})
    existing = await db.scalar(
        select(LearningModel).where(
            LearningModel.teacher_id == user.id, LearningModel.version == version
        )
    )
    if existing:
        return model_payload(existing)
    row = LearningModel(
        id=uuid4(), teacher_id=user.id, version=version, artifact=artifact, **body.model_dump()
    )
    db.add(row)
    audit(
        db,
        user,
        "LEARNING_MODEL_TRAINED",
        "learning_model",
        row.id,
        {"version": version, "dataset_sha256": artifact["dataset_sha256"], **body.model_dump()},
    )
    await db.commit()
    return model_payload(row)


class ForecastInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: UUID
    difficulty: int = Field(ge=1, le=10)
    task_mode: Literal["CARD_ACTIONS", "CARD_ENTRY"]


@router.post("/teacher/analytics/forecast", status_code=201)
async def forecast(body: ForecastInput, db: DB, user: TeacherOnly) -> dict[str, Any]:
    await learning_student(db, user, body.student_id)
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    model = await db.scalar(
        select(LearningModel)
        .where(LearningModel.teacher_id == user.id)
        .order_by(LearningModel.created_at.desc(), LearningModel.id)
        .limit(1)
    )
    if model is None:
        raise APIError(409, "MODEL_UNAVAILABLE", "Сначала подготовьте модель на истории занятий.")
    history = await records(db, user.id, body.student_id)
    try:
        values = features(history, body.difficulty, body.task_mode)
        predicted = predict(model.artifact, values)
    except ValueError as error:
        raise APIError(400, "INSUFFICIENT_DATA", str(error)) from error
    source_hash = digest(
        {"history": history, "request": body.model_dump(mode="json"), "model": model.version}
    )
    latest = await db.scalar(
        select(LearningForecast)
        .where(
            LearningForecast.teacher_id == user.id, LearningForecast.student_id == body.student_id
        )
        .order_by(LearningForecast.created_at.desc(), LearningForecast.id)
        .limit(1)
    )
    if latest and latest.payload["source_hash"] == source_hash:
        return {"id": str(latest.id), "created_at": latest.created_at, **latest.payload}
    advice = {
        "timeliness": "Отработайте приём и первичный статус с серверным таймером.",
        "correctness": "Повторите выбор профильной службы и допустимую цепочку статусов.",
        "completeness": "Проверьте обязательные поля, комментарии и доклад дежурному.",
    }
    ordered_axes = sorted(AXES, key=lambda axis: predicted[axis])
    payload = {
        "model_version": model.version,
        "dataset_kind": model.dataset_kind,
        "predicted_axes": predicted,
        "difficulty": body.difficulty,
        "task_mode": body.task_mode,
        "recommendations": [advice[axis] for axis in ordered_axes if predicted[axis] < 80],
        "source_hash": source_hash,
        "history_count": len(history),
        "validation_mae": model.artifact["mae_overall"],
        "beats_baseline": model.artifact["beats_baseline"],
        "converged": model.artifact["converged"],
        "scope": "Прогноз следующего задания заданного вида и сложности. "
        "Регламентный балл не изменяется.",
    }
    row = LearningForecast(
        id=uuid4(),
        model_id=model.id,
        teacher_id=user.id,
        student_id=body.student_id,
        payload=payload,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    audit(
        db,
        user,
        "LEARNING_FORECAST_CREATED",
        "learning_forecast",
        row.id,
        {
            "student_id": str(body.student_id),
            "model_version": model.version,
            "source_hash": source_hash,
        },
    )
    await db.commit()
    return {"id": str(row.id), "created_at": row.created_at, **payload}


@router.get("/learning/forecasts")
async def forecasts(db: DB, user: TrainingUser, student_id: UUID | None = None) -> dict[str, Any]:
    identifier = await learning_student(db, user, student_id)
    query = select(LearningForecast).where(LearningForecast.student_id == identifier)
    if user.role == "TEACHER":
        query = query.where(LearningForecast.teacher_id == user.id)
    rows = list(
        await db.scalars(
            query.order_by(LearningForecast.created_at.desc(), LearningForecast.id).limit(100)
        )
    )
    items = []
    for row in rows:
        # A prospective comparison uses the first matching task DELIVERED after prediction.
        # An already opened/completed card cannot become evidence of forecast accuracy.
        actual = await db.scalar(
            select(Assignment)
            .join(Lesson)
            .join(Scenario, Scenario.id == Assignment.scenario_id)
            .where(
                Assignment.student_id == identifier,
                Lesson.teacher_id == row.teacher_id,
                Assignment.delivered_at > row.created_at,
                Assignment.score.is_not(None),
                Assignment.task_mode == row.payload["task_mode"],
                Scenario.difficulty == row.payload["difficulty"],
            )
            .order_by(Assignment.delivered_at, Assignment.id)
            .limit(1)
        )
        observed = (
            {axis: actual.score["axes"][axis]["score"] for axis in AXES}
            if actual and actual.score
            else None
        )
        items.append(
            {
                "id": str(row.id),
                "created_at": row.created_at,
                **row.payload,
                "actual_axes": observed,
                "actual_assignment_id": str(actual.id) if actual else None,
                "absolute_error": {
                    axis: round(abs(observed[axis] - row.payload["predicted_axes"][axis]), 2)
                    for axis in AXES
                }
                if observed
                else None,
            }
        )
    return {"items": items}
