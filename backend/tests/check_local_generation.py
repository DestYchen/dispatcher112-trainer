import asyncio
import json
from pathlib import Path
from time import perf_counter
from uuid import UUID

from sqlalchemy import select

from app.api.generation import GenerationInput
from app.db.base import session_factory
from app.db.models import AuditLog, Lesson, User
from app.generation.builder import build_scenario


async def main() -> None:
    fixture = json.loads(
        await asyncio.to_thread(Path("/data/learning-acceptance.json").read_text, encoding="utf-8")
    )
    rows = []
    async with session_factory() as db:
        teacher = (await db.scalars(select(User).where(User.login == fixture["teacher"]))).one()
        lesson = await db.get(Lesson, UUID(fixture["lesson_id"]))
        assert lesson
        request = GenerationInput(
            count=10,
            difficulty_range=(1, 10),
            generation_backend="local_llm",
            origin_mix={"OPERATOR_112": 1, "EXTERNAL_SYSTEM": 0},
        ).model_dump(mode="json")
        for index in range(10):
            started = perf_counter()
            scenario = await build_scenario(db, lesson.id, teacher.id, request, 112 + index)
            # The acceptance fixture is finished; these independent drafts are for review only.
            scenario.card_payload = {
                key: value for key, value in scenario.card_payload.items() if key != "lesson_id"
            }
            db.add(scenario)
            await db.flush()
            db.add(
                AuditLog(
                    user_id=teacher.id,
                    action="LOCAL_LLM_ACCEPTANCE",
                    entity_type="scenario",
                    entity_id=scenario.id,
                    payload={"synthetic": True, "generation": scenario.card_payload["generation"]},
                )
            )
            rows.append(
                {
                    "id": str(scenario.id),
                    "status": scenario.status,
                    "description": scenario.card_payload["description"],
                    "checks": scenario.card_payload["validation"],
                    "seconds": round(perf_counter() - started, 2),
                    "generation": scenario.card_payload["generation"],
                }
            )
            await db.commit()
            print(
                f"Prepared {index + 1}/10: {scenario.status}, {rows[-1]['seconds']} s", flush=True
            )
    output = {
        "samples": rows,
        "accepted_for_review": sum(row["status"] == "PENDING_REVIEW" for row in rows),
        "all_automatic_checks": sum(all(check["ok"] for check in row["checks"]) for row in rows),
    }
    await asyncio.to_thread(
        Path("/data/local-generation-acceptance.json").write_text,
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if output["accepted_for_review"] < 8:
        raise RuntimeError("Too many rejected local model drafts")
    print(
        "Local generation acceptance:",
        output["accepted_for_review"],
        "of 10 drafts ready for review",
    )


if __name__ == "__main__":
    asyncio.run(main())
