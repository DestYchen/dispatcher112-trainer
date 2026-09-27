import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.transport_security import http_verify

shared_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class GrammarResult:
    available: bool
    items: tuple[dict[str, Any], ...]
    reason: str | None = None


pending_checks: dict[tuple[str, str], asyncio.Task[GrammarResult]] = {}


async def check_grammar(text: str, field: str = "comment") -> GrammarResult:
    # Share only an in-flight identical request; completed results are never cached here.
    # A later request still contacts LanguageTool and detects an outage immediately.
    key = (text, field)
    task = pending_checks.get(key)
    if task is None:
        task = asyncio.create_task(request_grammar(text, field))
        pending_checks[key] = task
        task.add_done_callback(lambda completed: pending_checks.pop(key, None))
    return await asyncio.shield(task)


async def request_grammar(text: str, field: str) -> GrammarResult:
    try:
        if shared_client is None:
            async with httpx.AsyncClient(
                timeout=1.5, trust_env=False, verify=http_verify()
            ) as client:
                response = await client.post(
                    settings.languagetool_url + "/v2/check",
                    data={"text": text, "language": "ru-RU"},
                )
        else:
            response = await shared_client.post(
                settings.languagetool_url + "/v2/check", data={"text": text, "language": "ru-RU"}
            )
        response.raise_for_status()
        payload = response.json()
        items = []
        encoded = text.encode("utf-16-le")
        for match in payload["matches"]:
            offset, length = int(match["offset"]), int(match["length"])
            if offset < 0 or length < 0 or (offset + length) * 2 > len(encoded):
                raise ValueError("LanguageTool returned invalid text offsets")
            items.append(
                {
                    "kind": "SPELLING",
                    "severity": "CRITICAL" if field == "address" else "MINOR",
                    "offset": offset,
                    "length": length,
                    "text": encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le"),
                    "message": str(match["message"]),
                    "suggestions": [
                        str(item["value"]) for item in match.get("replacements", [])[:5]
                    ],
                    "rule_id": str(match["rule"]["id"]),
                    "field": field,
                }
            )
        return GrammarResult(True, tuple(items))
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
        return GrammarResult(
            False,
            (),
            "Сервис проверки грамотности недоступен; его вес распределён между остальными осями.",
        )
