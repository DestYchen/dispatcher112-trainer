import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IncidentType, Street
from app.domain.classifier import normalize_street


async def validate_scenario(
    db: AsyncSession, payload: dict[str, Any], incident: IncidentType, origin: str
) -> list[dict[str, Any]]:
    street = str(payload.get("address", {}).get("raw", "")).split(",")[0].strip()
    found = await db.scalar(select(Street.id).where(Street.name_norm == normalize_street(street)))
    attributes = payload.get("attributes", [])
    valid_attributes = (
        attributes == list(incident.attributes.values())
        if origin == "OPERATOR_112"
        else not attributes
    )
    description = str(payload.get("description", ""))
    modifiers = payload.get("modifiers", [])
    checks = [
        {
            "code": "STREET",
            "ok": found is not None,
            "action": "REJECT",
            "message": "Улица найдена в справочнике." if found else "Улицы нет в справочнике.",
        },
        {
            "code": "ATTRIBUTES",
            "ok": valid_attributes,
            "action": "REJECT",
            "message": "Признаки соответствуют типу."
            if valid_attributes
            else "Признаки не соответствуют типу.",
        },
        {
            "code": "DESCRIPTION_LENGTH",
            "ok": 40 <= len(description) <= 400,
            "action": "REGENERATE",
            "message": "Описание должно содержать 40–400 символов.",
        },
    ]
    contradictions = []
    text = description.lower().replace("ё", "е")
    if "VICTIMS" in modifiers and (
        "пострадавших нет" in text or not re.search(r"пострада|ранен|травм", text)
    ):
        contradictions.append("Есть пострадавшие")
    patterns = {
        "THREAT_TO_PEOPLE": r"угроз.*люд|опасност.*люд",
        "FATALITIES": r"погиб|смерт",
        "NO_ACCESS": r"доступ.*закрыт|нет доступа|недоступ",
        "ROAD_BLOCKED": r"движение.*перекрыт|дорог.*перекрыт",
        "CHILD_INVOLVED": r"ребен|дет",
    }
    for modifier, pattern in patterns.items():
        if modifier in modifiers and not re.search(pattern, text):
            contradictions.append(modifier)
    checks.append(
        {
            "code": "MODIFIERS",
            "ok": not contradictions,
            "action": "REVIEW",
            "message": "Описание согласовано с модификаторами."
            if not contradictions
            else "Проверьте модификаторы в описании: " + ", ".join(contradictions),
        }
    )
    cleaned = description
    applicant = payload.get("applicant", {})
    for allowed in (applicant.get("name"), applicant.get("phone")):
        if allowed:
            cleaned = cleaned.replace(allowed, "")
    contains_pii = bool(
        re.search(
            r"(?:\+7|8)[\s(\d)-]{9,}|\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\b|\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.",
            cleaned,
        )
    )
    checks.append(
        {
            "code": "PERSONAL_DATA",
            "ok": not contains_pii,
            "action": "REJECT",
            "message": "Посторонних персональных данных нет."
            if not contains_pii
            else "В описании найдены неразрешённые ФИО или телефон.",
        }
    )
    return checks
