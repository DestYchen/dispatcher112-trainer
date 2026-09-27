import hashlib
import ipaddress
import json
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.transport_security import http_verify


class TextGenerator(Protocol):
    async def generate(self, prompt: str, *, max_tokens: int = 300) -> str:
        """Return description text; concrete generators implement this contract."""
        ...


class TemplateGenerator:
    async def generate(self, prompt: str, *, max_tokens: int = 300) -> str:
        data = json.loads(prompt)
        facts = {
            "THREAT_TO_PEOPLE": "Существует угроза людям.",
            "VICTIMS": "Есть пострадавшие, требуется помощь.",
            "FATALITIES": "Сообщается о погибших.",
            "NO_ACCESS": "Доступ к месту происшествия закрыт.",
            "ROAD_BLOCKED": "Движение перекрыто.",
            "CHILD_INVOLVED": "На месте находится ребёнок.",
        }
        modifiers = data.get("modifiers", [])
        detail = " ".join(facts[modifier] for modifier in modifiers)
        if "VICTIMS" not in modifiers and "FATALITIES" not in modifiers:
            detail += " Пострадавших нет."
        variants = [
            "Заявитель просит направить специалистов.",
            "Ситуация сохраняется, требуется проверка на месте.",
            "Сведения уточнены повторным обращением заявителя.",
        ]
        feedback = data.get("review_comment", "") or ""
        index = int(hashlib.sha256(feedback.encode()).hexdigest(), 16) % len(variants)
        text = (
            f"Поступило сообщение: {data['incident_type_name']}. {detail.strip()} {variants[index]}"
        )
        if feedback:
            text += " Первоначальные сведения требуют дополнительного уточнения."
        return text


def validate_local_llm_url() -> str:
    parsed = urlparse(settings.local_llm_url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Нужен адрес локального сервера Ollama по HTTP или HTTPS.")
    if "." in host and host not in {"host.docker.internal"}:
        try:
            local = ipaddress.ip_address(host).is_private
        except ValueError:
            local = host.endswith(".local")
        if not local:
            raise ValueError("Внешний сервер генерации запрещён.")
    return settings.local_llm_url.rstrip("/")


class LocalLLMGenerator:
    async def generate(self, prompt: str, *, max_tokens: int = 300) -> str:
        url = validate_local_llm_url()
        async with httpx.AsyncClient(timeout=120, trust_env=False, verify=http_verify()) as client:
            response = await client.post(
                url + "/api/generate",
                json={
                    "model": settings.local_llm_model,
                    "stream": False,
                    "system": "Составь только описание происшествия на русском, 40–400 символов. "
                    "Строго соблюдай заданные факты и модификаторы. Не добавляй ФИО, телефоны, "
                    "адреса, службы или номера карточек. Не придумывай причины, масштаб, ущерб "
                    "и последствия. Учти замечание преподавателя. "
                    "Если факта нет, не утверждай его.",
                    "prompt": prompt,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": 0.2,
                        "num_ctx": 2048,
                        "num_gpu": 0,
                        "num_thread": 4,
                        "seed": int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16),
                    },
                },
            )
            response.raise_for_status()
            text = response.json()["response"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Локальная модель не вернула описание.")
            return text.strip()
