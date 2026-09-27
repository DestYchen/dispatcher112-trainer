from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.lesson_settings import LessonSettings
from app.scoring.engine import evaluate
from app.scoring.grammar import check_grammar
from app.scoring.rules.address import check_addresses
from app.scoring.rules.lexicon import satisfies
from app.scoring.rules.timeliness import deadline_score
from app.scoring.types import ScoreInput
from app.scoring.violations import REGISTRY


def perfect() -> ScoreInput:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    chain = ["ACCEPTED", "RESPONSE_STARTED", "ARRIVED", "WORK_IN_PROGRESS", "WORK_COMPLETED"]
    return ScoreInput(
        delivered_at=now,
        opened_at=now + timedelta(seconds=2),
        primary_status_at=now + timedelta(seconds=24),
        closed_at=now + timedelta(seconds=100),
        settings=LessonSettings().model_dump(mode="json"),
        reference={
            "expected_status": "ACCEPTED",
            "expected_status_chain": chain,
            "comment_required": False,
            "comment_must_contain": [],
            "report_required": False,
        },
        events=tuple(
            {"status": status, "comment": None, "is_automatic": False} for status in chain
        ),
        services=("МЧС", "ДДС Чертаново Южное"),
        card_number="2026-0919-000001",
        address="Дубнинская улица, д. 28",
        incident_type_name="пожар: частный дом",
    )


def codes(data: ScoreInput) -> set[str]:
    return {item["code"] for item in evaluate(data)["violations"]}


def test_one_hundred_runs_are_identical_including_metadata() -> None:
    data = perfect()
    first = evaluate(data)
    assert first["total"] == 100
    for _ in range(100):
        assert evaluate(data) == first
    assert first["engine_version"] == "1.0.0"
    assert first["computed_at"] == data.closed_at.isoformat()


@pytest.mark.parametrize(
    "delay, expected",
    [(0, 100), (24000, 100), (30000, 100), (45000, 70), (60000, 40), (70000, 0), (None, 0)],
)
def test_exact_deadline_curve(delay: int | None, expected: float) -> None:
    assert deadline_score(delay, 30000) == expected


def test_weighted_two_timers_and_hint_mode() -> None:
    data = perfect()
    delayed = replace(data, primary_status_at=data.delivered_at + timedelta(seconds=45))
    assert evaluate(delayed)["axes"]["timeliness"]["score"] == 82
    late = replace(
        data,
        primary_status_at=data.delivered_at + timedelta(seconds=70),
        closed_at=data.delivered_at + timedelta(seconds=400),
    )
    assert evaluate(late)["axes"]["timeliness"]["score"] == 0
    late = replace(late, settings={**late.settings, "hints_enabled": True})
    result = evaluate(late)
    assert result["axes"]["timeliness"]["score"] == 100
    assert not any(item["axis"] == "timeliness" for item in result["violations"])


def test_each_correctness_rule_has_its_own_weight() -> None:
    data = perfect()
    refused = replace(
        data, events=({"status": "NOT_ACCEPTED", "comment": "Передано в МЧС по компетенции."},)
    )
    result = evaluate(refused)
    assert result["axes"]["correctness"]["score"] == 0
    item = next(item for item in result["violations"] if item["code"] == "REFUSED_OWN_INCIDENT")
    assert item["severity"] == "CRITICAL"
    skipped = replace(data, events=(data.events[0], data.events[-1]))
    assert evaluate(skipped)["axes"]["correctness"]["score"] == 85
    unfinished = replace(data, events=data.events[:-1])
    assert evaluate(unfinished)["axes"]["correctness"]["score"] == 90
    wrong = replace(
        data,
        reference={
            **data.reference,
            "expected_status": "NOT_ACCEPTED",
            "expected_status_chain": ["NOT_ACCEPTED"],
        },
    )
    assert evaluate(wrong)["axes"]["correctness"]["score"] == 40


def test_required_comments_and_semantic_entities() -> None:
    data = perfect()
    reference = {
        **data.reference,
        "expected_status": "NOT_ACCEPTED",
        "expected_status_chain": ["NOT_ACCEPTED"],
        "comment_required": True,
        "comment_must_contain": ["reason", "handed_to"],
    }
    missing = replace(
        data, reference=reference, events=({"status": "NOT_ACCEPTED", "comment": None},)
    )
    assert {"MISSING_COMMENT", "INCOMPLETE_COMMENT"} <= codes(missing)
    incomplete = replace(missing, events=({"status": "NOT_ACCEPTED", "comment": "не обслуживаем"},))
    assert "INCOMPLETE_COMMENT" in codes(incomplete)
    complete = replace(
        missing,
        events=(
            {
                "status": "NOT_ACCEPTED",
                "comment": "Объект не обслуживаем. Информация передана в МЧС.",
            },
        ),
    )
    assert evaluate(complete)["axes"]["completeness"]["score"] == 100


@pytest.mark.parametrize(
    "requirement,text,expected",
    [
        ("reason", "Повторная карточка", True),
        ("reason", "Всё хорошо", False),
        ("handed_to", "Передано в ПИК", True),
        ("handed_to", "Передано кому-то", False),
        ("handed_to", "МЧС", False),
        ("card_number", "Дубликат 2026-0919-000001", True),
        ("card_number", "Номер 12", False),
        ("clarified_address", "Уточнено: Дубнинская улица, д. 28", True),
        ("clarified_address", "Там же", False),
    ],
)
def test_semantic_dictionary(requirement: str, text: str, expected: bool) -> None:
    assert satisfies(requirement, text, ()) is expected


def test_phone_report_requires_recipient_and_all_reference_entities() -> None:
    data = perfect()
    data = replace(
        data,
        reference={
            **data.reference,
            "report_required": True,
            "report_callee_code": "DUTY_OFFICER",
            "report_must_mention": ["address", "incident_type", "victims"],
        },
    )
    assert "MISSING_REPORT" in codes(data)
    wrong = replace(
        data,
        reports=(
            {
                "callee_code": "HEAD_ENGINEER",
                "transcript": "Пожар, Дубнинская улица, 28, пострадавших нет.",
            },
        ),
    )
    assert "MISSING_REPORT" in codes(wrong)
    good = replace(
        data,
        reports=(
            {
                "callee_code": "DUTY_OFFICER",
                "transcript": "Пожар, Дубнинская улица, 28, пострадавших нет.",
            },
        ),
    )
    assert "MISSING_REPORT" not in codes(good)
    missing = replace(data, reports=({"callee_code": "DUTY_OFFICER", "transcript": "Пожар"},))
    assert "MISSING_REPORT" in codes(missing)


def test_literacy_penalties_skip_redistribution_and_floor() -> None:
    data = replace(
        perfect(),
        grammar_items=({"kind": "SPELLING", "severity": "MINOR"},) * 2,
        address_items=({"kind": "ADDRESS_TYPO", "severity": "CRITICAL"},),
    )
    assert evaluate(data)["axes"]["literacy"]["score"] == 65
    assert (
        evaluate(replace(data, address_items=data.address_items * 5))["axes"]["literacy"]["score"]
        == 0
    )
    skipped = evaluate(replace(data, grammar_available=False))
    assert skipped["axes"]["literacy"]["skipped"]
    assert skipped["axes"]["literacy"]["score"] is None
    assert sum(item["weight"] for item in skipped["axes"].values()) == pytest.approx(1)
    assert skipped["total"] == 100


@pytest.mark.parametrize("code", sorted(REGISTRY))
def test_violation_messages_are_russian_and_actionable(code: str) -> None:
    _, _, message, hint = REGISTRY[code]
    assert len(message) > 15 and len(hint) > 15
    assert any("а" <= char.lower() <= "я" for char in message)
    assert any("а" <= char.lower() <= "я" for char in hint)


async def test_real_trigram_address_context_and_inflection(db: AsyncSession) -> None:
    issues = await check_addresses(db, "Передано на Дубининской улице", "Дубнинская улица, 28")
    assert len(issues) == 1 and issues[0]["kind"] == "ADDRESS_TYPO"
    assert issues[0]["severity"] == "CRITICAL"
    assert issues[0]["suggestions"][0] == "Дубнинская улица"
    assert issues[0]["text"] == "Дубининской"
    assert not await check_addresses(db, "Дубининская улица", "Дубининская улица, 28")
    assert not await check_addresses(db, "Дубнинская, не Дубининская", "Дубнинская улица, 28")
    unknown = await check_addresses(db, "улица Несуществующая", "Дубнинская улица, 28")
    assert unknown[0]["kind"] == "UNKNOWN_STREET"
    typo = await check_addresses(db, "Дубннинская улица, 28", "Дубнинская улица, 28")
    assert typo[0]["kind"] == "ADDRESS_TYPO"
    assert "Дубнинская улица" in typo[0]["suggestions"]


async def test_language_service_timeout_degrades_without_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("unavailable")

    monkeypatch.setattr(httpx.AsyncClient, "post", unavailable)
    result = await check_grammar("Текст для проверки")
    assert not result.available and result.items == () and result.reason
