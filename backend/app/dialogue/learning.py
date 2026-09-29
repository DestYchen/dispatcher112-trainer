"""Overnight learning for the boss-call graph, always gated by the teacher.

1. Night job (`python -m app.dialogue.learning`): take phrases the graph did not understand,
   ask the local LLM (Ollama, CPU is fine because nobody waits) which report slot each one
   fills, or what short line the duty officer should answer. Results go to proposals.json.
2. The teacher reviews proposals in the console and approves or rejects each one.
3. Approval merges the phrase into the graph as an example (or a new small-talk line). Nothing
   the model proposes reaches students without that approval.
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.base import session_factory
from app.db.models import InteractionEvent

DIALOGUE_DIR = Path("/data/dialogue")
GRAPH = DIALOGUE_DIR / "boss_graph.json"
PROPOSALS = DIALOGUE_DIR / "proposals.json"
SLOTS = ("address", "incident_type", "victims", "measures")
PROMPT = """Ты помогаешь обучать тренажёр диспетчера. Диспетчер ДДС докладывает руководителю о происшествии.
Руководитель перед этим спросил: «{asked}». Диспетчер ответил: «{utterance}».
Какие сведения содержит ответ? Возможные значения slot: address (адрес), incident_type (что произошло),
victims (есть ли пострадавшие), measures (кто выехал, какие меры), none (ничего из этого).
Если slot = none, предложи короткую реплику руководителя (до 10 слов), чтобы вернуть разговор к докладу.
Ответь JSON: {{"slot": "...", "reply": "..."}}"""


def read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write(path: Path, value: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def known(graph: dict[str, Any], proposals: list[dict[str, Any]]) -> set[str]:
    seen = {e.lower() for examples in graph.get("examples", {}).values() for e in examples}
    seen |= {row["utterance"].lower() for row in graph.get("smalltalk", [])}
    seen |= {p["utterance"].lower() for p in proposals}
    return seen


async def ask_llm(client: httpx.AsyncClient, utterance: str, asked: str) -> dict[str, str]:
    response = await client.post(
        f"{settings.local_llm_url.rstrip('/')}/api/generate",
        json={"model": settings.local_llm_model, "prompt": PROMPT.format(utterance=utterance, asked=asked),
              "format": "json", "stream": False, "options": {"temperature": 0}},
        timeout=300,
    )
    response.raise_for_status()
    data = json.loads(response.json()["response"])
    slot = str(data.get("slot", "none")).strip()
    reply = re.sub(r"\s+", " ", str(data.get("reply", ""))).strip()[:120]
    return {"slot": slot if slot in SLOTS else "none", "reply": reply}


async def night() -> int:
    graph = read(GRAPH, {})
    proposals: list[dict[str, Any]] = read(PROPOSALS, [])
    texts = {n: spec["text"] for n, spec in graph["nodes"].items()}
    async with session_factory() as db:
        rows = list(await db.scalars(select(InteractionEvent).where(InteractionEvent.kind == "DIALOGUE_UNMATCHED")))
    seen = known(graph, proposals)
    counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        utterance = (row.payload or {}).get("utterance", "").strip()
        if utterance and utterance.lower() not in seen:
            item = counts.setdefault(utterance.lower(), {"utterance": utterance, "asked": row.payload["asked"], "count": 0})
            item["count"] += 1
    added = 0
    async with httpx.AsyncClient() as client:
        for item in sorted(counts.values(), key=lambda x: -x["count"]):
            try:
                guess = await ask_llm(client, item["utterance"], texts.get(item["asked"], ""))
            except (httpx.HTTPError, ValueError, KeyError) as error:
                guess = {"slot": "none", "reply": "", "error": str(error)[:200]}
            proposals.append({"id": str(uuid4()), "status": "PENDING", "created_at": datetime.now(UTC).isoformat(),
                              **item, **guess})
            added += 1
    write(PROPOSALS, proposals)
    return added


def decide(proposal_id: str, approve: bool, slot: str | None, reply: str | None, teacher: str) -> dict[str, Any]:
    proposals: list[dict[str, Any]] = read(PROPOSALS, [])
    proposal = next((p for p in proposals if p["id"] == proposal_id), None)
    if proposal is None or proposal["status"] != "PENDING":
        raise KeyError(proposal_id)
    final_slot = slot or proposal["slot"]
    final_reply = (reply if reply is not None else proposal.get("reply", "")).strip()
    if approve:
        graph = read(GRAPH, {})
        if final_slot in SLOTS:
            graph.setdefault("examples", {}).setdefault(final_slot, []).append(proposal["utterance"])
        elif final_reply:
            graph.setdefault("smalltalk", []).append({"utterance": proposal["utterance"], "reply": final_reply})
        graph["version"] = int(graph.get("version", 1)) + 1
        write(GRAPH, graph)
    proposal.update(status="APPROVED" if approve else "REJECTED", slot=final_slot, reply=final_reply,
                    decided_by=teacher, decided_at=datetime.now(UTC).isoformat())
    write(PROPOSALS, proposals)
    return proposal


if __name__ == "__main__":
    print({"proposals_added": asyncio.run(night())})
