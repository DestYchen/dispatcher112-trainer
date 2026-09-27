from typing import Any

from app.scoring.rules.incomplete_comment import missing_entities
from app.scoring.rules.missing_comment import missing_comment
from app.scoring.rules.primary_status import wrong_primary
from app.scoring.rules.progress import skipped_progress
from app.scoring.rules.refused_own import refused_own
from app.scoring.rules.report import report_complete
from app.scoring.rules.status_mismatch import status_mismatch
from app.scoring.rules.timeliness import deadline_score
from app.scoring.types import ScoreInput
from app.scoring.violations import violation

ENGINE_VERSION = "1.0.0"


def evaluate(data: ScoreInput) -> dict[str, Any]:
    at = data.closed_at.isoformat()
    primary_ms = (
        round((data.primary_status_at - data.delivered_at).total_seconds() * 1000)
        if data.primary_status_at
        else None
    )
    processing_ms = (
        round((data.closed_at - data.opened_at).total_seconds() * 1000) if data.opened_at else None
    )
    primary_deadline = data.settings["primary_status_deadline_sec"] * 1000
    processing_deadline = data.settings["card_processing_deadline_sec"] * 1000
    primary = deadline_score(primary_ms, primary_deadline)
    processing = deadline_score(processing_ms, processing_deadline)
    scores = {
        "timeliness": primary * 0.6 + processing * 0.4,
        "correctness": 100.0,
        "completeness": 100.0,
        "literacy": 100.0,
    }
    violations = []
    if data.settings.get("hints_enabled"):
        scores["timeliness"] = 100.0
    else:
        if primary_ms is None or primary_ms > primary_deadline:
            violations.append(violation("PRIMARY_DEADLINE_EXCEEDED", at, field="primary_status"))
        if processing_ms is None or processing_ms > processing_deadline:
            violations.append(violation("PROCESSING_DEADLINE_EXCEEDED", at, field="processing"))
    if data.entry_fields is None:
        missing = missing_entities(data)
        for failed, code, penalty in [
            (wrong_primary(data), "WRONG_PRIMARY_STATUS", 50),
            (refused_own(data), "REFUSED_OWN_INCIDENT", 25),
            (skipped_progress(data), "SKIPPED_PROGRESS_STATUS", 15),
            (status_mismatch(data), "STATUS_MISMATCH", 10),
            (missing_comment(data), "MISSING_COMMENT", 40),
            (bool(missing), "INCOMPLETE_COMMENT", 35),
            (not report_complete(data), "MISSING_REPORT", 25),
        ]:
            if failed:
                item = violation(code, at)
                if code == "INCOMPLETE_COMMENT":
                    item["missing"] = missing
                violations.append(item)
                scores[item["axis"]] -= penalty
    if data.entry_fields is not None:
        fields = data.entry_fields
        scores["correctness"] = 100 * sum(item["correct"] for item in fields) / max(1, len(fields))
        scores["completeness"] = (
            100 * sum(item["present"] for item in fields) / max(1, len(fields))
            if data.card_submitted
            else 0
        )
        for item in fields:
            if not item["correct"]:
                violations.append(
                    {
                        "code": "CARD_FIELD_MISMATCH",
                        "axis": "correctness",
                        "severity": "CRITICAL" if item["field"] == "address.raw" else "MAJOR",
                        "field": item["field"],
                        "at": at,
                        "message": f"Поле «{item['label']}» отличается от эталона.",
                        "hint": "Сверьте сведения обращения и эталон в разборе карточки.",
                    }
                )
        if not data.card_submitted:
            violations.append(
                {
                    "code": "CARD_NOT_SUBMITTED",
                    "axis": "completeness",
                    "severity": "MAJOR",
                    "field": "card",
                    "at": at,
                    "message": "Карточка не сдана.",
                    "hint": "Сохраните заполненные поля и сдайте карточку до завершения занятия.",
                }
            )
    language_items = [*data.address_items, *data.grammar_items]
    critical = sum(item["severity"] == "CRITICAL" for item in language_items)
    minor = len(language_items) - critical
    scores["literacy"] = max(0.0, 100.0 - critical * 25 - minor * 5)
    for issue in language_items:
        violations.append(
            violation(
                issue["kind"],
                issue.get("at", at),
                **{key: value for key, value in issue.items() if key not in {"kind", "at"}},
            )
        )
    weights = dict(data.settings["weights"])
    available = data.grammar_available and data.settings.get("grammar_check_enabled", True)
    skipped = None
    if not available:
        skipped = data.grammar_skip_reason or (
            "Сервис проверки грамотности недоступен."
            if data.settings.get("grammar_check_enabled", True)
            else "Проверка грамотности отключена преподавателем."
        )
        weights["literacy"] = 0
        remaining = sum(weights.values())
        if remaining > 0:
            weights = {key: weight / remaining for key, weight in weights.items()}
        else:
            # Only literacy was weighted: use the three available axes equally.
            weights = {
                "timeliness": 1 / 3,
                "correctness": 1 / 3,
                "completeness": 1 / 3,
                "literacy": 0,
            }
    axes: dict[str, Any] = {
        key: {"score": round(value, 2), "weight": weights[key]} for key, value in scores.items()
    }
    if skipped:
        axes["literacy"].update(skipped=True, reason=skipped, score=None)
    return {
        "total": round(sum(scores[key] * weights[key] for key in scores), 2),
        "axes": axes,
        "violations": violations,
        "timings": {
            "primary_delay_ms": primary_ms,
            "processing_ms": processing_ms,
            "primary_deadline_ms": primary_deadline,
            "processing_deadline_ms": processing_deadline,
        },
        "grammar": {
            "critical": critical,
            "minor": minor,
            "items": language_items,
            "available": available,
            "skip_reason": skipped,
        },
        "computed_at": at,
        "engine_version": "entry-1.0.0" if data.entry_fields is not None else ENGINE_VERSION,
        **({"entry_fields": list(data.entry_fields)} if data.entry_fields is not None else {}),
    }
