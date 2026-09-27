import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.generation import GenerationInput
from app.config import settings
from app.db.models import GenerationJob, IncidentType, Scenario
from app.generation.builder import build_reference, build_scenario
from app.generation.llm import LocalLLMGenerator, TemplateGenerator
from app.generation.validation import validate_scenario
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_template_generates_forty_valid_cards_without_models(db: AsyncSession) -> None:
    lesson, _ = await lesson_fixture(db, 0)
    lesson.status = "PLANNED"
    request = GenerationInput(count=40).model_dump(mode="json")
    rows = [
        await build_scenario(db, lesson.id, lesson.teacher_id, request, index)
        for index in range(40)
    ]
    assert sum(row.status == "REJECTED" for row in rows) < 10
    assert all(row.status == "PENDING_REVIEW" for row in rows)
    assert all(40 <= len(row.card_payload["description"]) <= 400 for row in rows)
    assert len({row.card_payload["address"]["raw"] for row in rows}) > 20
    for row in rows:
        assert all(item["ok"] for item in row.card_payload["validation"])
        if row.origin == "EXTERNAL_SYSTEM":
            assert not row.card_payload["attributes"] and not row.card_payload["modifiers"]


def test_reference_is_deterministic_and_service_dependent() -> None:
    first = build_reference(["MCHS", "DDS_CHERTANOVO"], "DDS_CHERTANOVO")
    assert all(
        build_reference(["MCHS", "DDS_CHERTANOVO"], "DDS_CHERTANOVO") == first for _ in range(100)
    )
    assert first["expected_status"] == "ACCEPTED" and first["report_required"]
    other = build_reference(["MCHS"], "DDS_CHERTANOVO")
    assert other["expected_status"] == "NOT_ACCEPTED"
    assert other["comment_must_contain"] == ["reason", "handed_to"]


async def test_validation_rejects_unknown_street_wrong_attributes_and_pii_and_flags_modifiers(
    db: AsyncSession,
) -> None:
    _, rows = await lesson_fixture(db, 1)
    scenario = await db.get(Scenario, rows[0].scenario_id)
    assert scenario
    incident = await db.get(IncidentType, scenario.incident_type_id)
    assert incident
    payload = {
        **scenario.card_payload,
        "address": {"raw": "Несуществующая улица, д. 10"},
        "attributes": ["неверные"],
        "modifiers": ["VICTIMS", "NO_ACCESS"],
        "description": "Пожар. Пострадавших нет. "
        "Иванов Иван Иванович сообщил с телефона +7 999 123-45-67.",
    }
    checks = {
        row["code"]: row for row in await validate_scenario(db, payload, incident, "OPERATOR_112")
    }
    for key in ("STREET", "ATTRIBUTES", "PERSONAL_DATA"):
        assert not checks[key]["ok"] and checks[key]["action"] == "REJECT"
    assert not checks["MODIFIERS"]["ok"] and checks["MODIFIERS"]["action"] == "REVIEW"


async def test_length_failure_retries_three_times(db: AsyncSession) -> None:
    class ShortGenerator:
        calls = 0

        async def generate(self, prompt: str, *, max_tokens: int = 300) -> str:
            self.calls += 1
            return str(json.loads(prompt)["incident_type_name"])[:10]

    lesson, _ = await lesson_fixture(db, 0)
    generator = ShortGenerator()
    scenario = await build_scenario(
        db, lesson.id, lesson.teacher_id, GenerationInput().model_dump(mode="json"), 1, generator
    )
    assert generator.calls == 3 and scenario.status == "REJECTED"


@pytest.mark.parametrize("endpoint", ["http://ollama:11434", "https://ollama:11435"])
async def test_local_llm_uses_only_local_description_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    captured = []

    async def response(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.append((url, kwargs["json"]))
        return httpx.Response(
            200,
            json={"response": "Описание происшествия из локальной модели."},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    monkeypatch.setattr(settings, "local_llm_url", endpoint)
    result = await LocalLLMGenerator().generate("Условия", max_tokens=100)
    assert "локальной модели" in result
    assert captured[0][0] == endpoint + "/api/generate"
    assert captured[0][1]["options"]["num_predict"] == 100 and not captured[0][1]["stream"]


async def test_review_override_rejection_feedback_and_new_job(
    client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[UUID] = []
    feedback: list[dict[str, Any]] = []

    async def enqueue(job_id: UUID) -> None:
        queued.append(job_id)

    monkeypatch.setattr("app.api.generation.enqueue_generation", enqueue)
    monkeypatch.setattr("app.api.generation.append_feedback", feedback.append)
    lesson, _ = await lesson_fixture(db, 0)
    lesson.status = "PLANNED"
    request = GenerationInput().model_dump(mode="json")
    scenario = await build_scenario(db, lesson.id, lesson.teacher_id, request, 4)
    rejected = await build_scenario(db, lesson.id, lesson.teacher_id, request, 5)
    db.add_all([scenario, rejected])
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    reference = {**scenario.reference, "rationale": "Уточнено преподавателем по условиям карточки."}
    response = await client.post(
        f"/api/v1/teacher/scenarios/{scenario.id}/approve",
        json={"difficulty": 4, "reference": reference},
    )
    assert response.status_code == 200, response.text
    assert scenario.status == "APPROVED" and scenario.reference == reference
    result = await client.post(
        f"/api/v1/teacher/scenarios/{rejected.id}/reject",
        json={"comment": "Добавьте уточнение показаний заявителя."},
    )
    assert result.status_code == 200, result.text
    job_id = UUID(result.json()["job_id"])
    job = await db.get(GenerationJob, job_id)
    assert job and job.request["review_comment"] == rejected.review_comment
    assert queued == [job_id] and feedback[0]["comment"] == rejected.review_comment
    assert (await client.get(f"/api/v1/teacher/jobs/{job_id}")).json()["status"] == "QUEUED"
    template = TemplateGenerator()
    plain = await template.generate(json.dumps({"incident_type_name": "пожар", "modifiers": []}))
    improved = await template.generate(
        json.dumps(
            {
                "incident_type_name": "пожар",
                "modifiers": [],
                "review_comment": rejected.review_comment,
            }
        )
    )
    assert plain != improved and "уточнения" in improved
    assert len(list(await db.scalars(select(GenerationJob)))) == 1
