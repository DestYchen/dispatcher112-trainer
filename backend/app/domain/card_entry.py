import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApplicantInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=64)


class AddressInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    raw: str = Field(default="", max_length=500)
    clarification: str = Field(default="", max_length=1000)


class EntryCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    applicant: ApplicantInput = Field(default_factory=ApplicantInput)
    address: AddressInput = Field(default_factory=AddressInput)
    incident_type_id: UUID | None = None
    description: str = Field(default="", max_length=10000)
    modifiers: list[str] = Field(default_factory=list, max_length=6)
    notified_services: list[str] = Field(default_factory=list, max_length=100)


FIELD_LABELS = {
    "applicant.name": "Имя заявителя",
    "applicant.phone": "Телефон заявителя",
    "address.raw": "Адрес происшествия",
    "address.clarification": "Уточнение адреса",
    "incident_type_id": "Тип происшествия",
    "description": "Описание происшествия",
    "modifiers": "Особые признаки",
    "notified_services": "Службы оповещения",
}


def normalized(value: Any, field: str) -> Any:
    if isinstance(value, list):
        return sorted(set(value))
    text = str(value or "").casefold().replace("ё", "е")
    if field == "applicant.phone":
        return re.sub(r"[^\d*]", "", text)
    if field == "address.raw":
        text = re.sub(r"\b(?:ул|улица|дом|д)\b\.?", " ", text)
    return " ".join(re.findall(r"\w+", text))


def compare_fields(
    answer: dict[str, Any], expected: dict[str, Any], description_keywords: list[str]
) -> list[dict[str, Any]]:
    comparisons = []
    for field, label in FIELD_LABELS.items():
        path = field.split(".")
        actual: Any = answer
        target: Any = expected
        for part in path:
            actual = actual.get(part) if isinstance(actual, dict) else None
            target = target.get(part) if isinstance(target, dict) else None
        value, reference = normalized(actual, field), normalized(target, field)
        correct = value == reference
        if field == "description" and description_keywords:
            correct = all(
                re.search(r"(?<!\w)" + re.escape(normalized(word, field)) + r"(?!\w)", value)
                for word in description_keywords
            )
        comparisons.append(
            {
                "field": field,
                "label": label,
                "actual": actual,
                "expected": target,
                "correct": correct,
                "present": bool(value) or not bool(reference),
            }
        )
    return comparisons


def incoming_message(payload: dict[str, Any]) -> str:
    if payload.get("incoming_message"):
        return str(payload["incoming_message"])
    applicant, address = payload.get("applicant", {}), payload.get("address", {})
    return (
        f"Меня зовут {applicant.get('name', 'учебный заявитель')}. "
        f"Мой телефон: {applicant.get('phone', 'не указан')}. "
        f"Адрес: {address.get('raw', '')}. {address.get('clarification', '')}. "
        f"{payload.get('description', '')}"
    )
