import copy
import random
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, StatusEvent
from app.domain.criteria import assess_criteria
from app.domain.learning_model import AXES, digest, features, predict, train
from app.domain.lesson_settings import SuccessCriteria
from app.scoring.service import score_assignment
from tests.test_lessons import lesson_fixture


def synthetic_records() -> list[dict[str, Any]]:
    rng = random.Random(42)
    records = []
    for student in range(20):
        ability = rng.uniform(65, 90)
        for attempt in range(15):
            difficulty = rng.randrange(1, 10)
            values = {
                axis: round(
                    min(
                        100,
                        max(0, ability + attempt * 0.3 - difficulty * penalty + rng.uniform(-1, 1)),
                    ),
                    2,
                )
                for axis, penalty in zip(AXES, (2.5, 4, 3), strict=True)
            }
            records.append(
                {
                    "id": f"{student}/{attempt}",
                    "student_id": str(student),
                    "closed_at": (
                        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=attempt)
                    ).isoformat(),
                    "axes": values,
                    "difficulty": difficulty,
                    "task_mode": "CARD_ENTRY" if attempt % 2 else "CARD_ACTIONS",
                }
            )
    return records


def test_neural_model_holdout_accuracy_and_hundred_identical_predictions() -> None:
    records = synthetic_records()
    artifact = train(records)
    assert not set(artifact["training_students"]) & set(artifact["test_students"])
    assert artifact["training_count"] == 192 and artifact["test_count"] == 48
    assert len(artifact["layers"]) == 3
    assert artifact["mae_overall"] < 5 and artifact["beats_baseline"]
    for axis in AXES:
        errors = [
            abs(row["actual"][axis] - row["predicted"][axis]) for row in artifact["comparisons"]
        ]
        assert round(mean(errors), 2) == artifact["mae"][axis]
    history = [row for row in records if row["student_id"] == "0"]
    values = features(history, 4, "CARD_ACTIONS")
    first = predict(artifact, values)
    assert all(predict(artifact, values) == first for _ in range(100))
    assert train(list(reversed(records)))["version"] == artifact["version"]
    assert (
        predict(artifact, features(history, 9, "CARD_ACTIONS"))["correctness"]
        < first["correctness"]
    )
    assert digest(artifact) == digest(copy.deepcopy(artifact))


def test_forecast_features_use_only_prior_scores_and_keep_task_parameters() -> None:
    history = synthetic_records()[:5]
    before = features(history[:3], 2, "CARD_ENTRY")
    history[3]["axes"] = dict.fromkeys(AXES, 0)
    assert features(history[:3], 2, "CARD_ENTRY") == before
    assert before[-3:] == [0.2, 1.0, 0.15]
    with pytest.raises(ValueError):
        features(history[:2], 3, "CARD_ACTIONS")
    with pytest.raises(ValueError):
        features(history, 11, "CARD_ACTIONS")
    with pytest.raises(ValueError):
        train(history)


@pytest.mark.parametrize(
    "rules,texts,passed",
    [
        ({}, ["Принята."], True),
        ({"min_total": 81}, ["Принята."], False),
        ({"max_errors": 0}, ["Принята."], False),
        ({"max_spelling_errors": 0}, ["Принята."], False),
        ({"min_words": 3}, ["Принята."], False),
        ({"min_words": 1}, [], False),
        ({"sentence_end_required": True}, ["Принята"], False),
        ({"sentence_end_required": True}, ["Принята!"], True),
        ({"required_terms": ["адрес"]}, ["Адрес проверен."], True),
        ({"required_terms": ["адрес"]}, ["Адресат найден."], False),
    ],
)
def test_success_criteria_are_frozen_deterministic_and_do_not_change_score(
    rules: dict[str, Any],
    texts: list[str],
    passed: bool,
) -> None:
    score = {"total": 80, "violations": [{"code": "SPELLING"}]}
    original = copy.deepcopy(score)
    criteria = SuccessCriteria(**rules).model_dump()
    first = assess_criteria(score, texts, criteria)
    assert first["passed"] == passed and score == original
    assert all(assess_criteria(score, texts, criteria) == first for _ in range(100))


@pytest.mark.parametrize(
    "rules",
    [
        {"min_total": 101},
        {"max_errors": -1},
        {"required_terms": [".*"]},
        {"required_terms": ["ёлка", "Елка"]},
    ],
)
def test_criteria_validation(rules: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SuccessCriteria(**rules)


def test_unavailable_grammar_does_not_claim_complete_success_or_lower_score() -> None:
    score = {"total": 80, "violations": [], "grammar": {"available": False}}
    result = assess_criteria(score, ["Проверено."], SuccessCriteria().model_dump())
    assert result["passed"] is None and result["unavailable"] and score["total"] == 80


async def test_criteria_snapshot_is_saved_once_with_original_score(db: AsyncSession) -> None:
    lesson, assignments = await lesson_fixture(db, 1)
    now = datetime.now(UTC)
    row = assignments[0]
    row.delivered_at = now - timedelta(seconds=30)
    row.opened_at = now - timedelta(seconds=29)
    row.primary_status_at = now - timedelta(seconds=28)
    row.closed_at, row.state = now, "CLOSED"
    lesson.settings = {
        **lesson.settings,
        "grammar_check_enabled": False,
        "success_criteria": SuccessCriteria(min_total=0, max_errors=1000, min_words=5).model_dump(),
    }
    db.add(
        StatusEvent(
            assignment_id=row.id,
            status="ACCEPTED",
            comment="Проверено.",
            elapsed_ms=2000,
            is_automatic=False,
            created_at=row.primary_status_at,
        )
    )
    await db.flush()
    first = copy.deepcopy(await score_assignment(db, row, lesson))
    assert first["criteria"]["passed"] is False
    assert any("недостаточно слов" in error for error in first["criteria"]["errors"])
    assert first["timings"]["primary_delay_ms"] == 2000
    lesson.settings = {**lesson.settings, "success_criteria": None}
    assert await score_assignment(db, row, lesson) == first
    await db.flush()
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "SCORE_COMPUTED", AuditLog.entity_id == row.id)
        )
        == 1
    )
