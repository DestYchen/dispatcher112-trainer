import re
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Street
from app.domain.classifier import normalize_street, street_candidates

TRAP_STREETS = {"дубнинская улица", "дубининская улица"}
STREET_WORD = r"(?:ул\.?|улиц[аыуе]|шоссе|проспект|переулок|проезд|бульвар)"
MENTIONS = re.compile(
    r"(?:(?:ул\.?|улиц[аыуе])\s+(?P<before>[А-ЯЁ][А-Яа-яЁё-]+)|(?P<after>[А-ЯЁ][А-Яа-яЁё-]+)\s+"
    + STREET_WORD
    + r")"
)


def nominative(value: str) -> str:
    return re.sub(r"(?:ской|скую|ская)$", "ская", value, flags=re.I)


@lru_cache(maxsize=4096)
def street_forms(name: str) -> tuple[str, ...]:
    bare = normalize_street(name).removesuffix(" улица")
    forms = [bare]
    if bare.endswith("ая"):
        forms.extend([bare[:-2] + ending for ending in ("ой", "ую", "ою")])
    if bare.endswith("яя"):
        forms.extend([bare[:-2] + ending for ending in ("ей", "юю", "ею")])
    return tuple(forms)


def utf16_offset(text: str, offset: int) -> int:
    return len(text[:offset].encode("utf-16-le")) // 2


async def check_addresses(
    db: AsyncSession, text: str, expected_address: str
) -> list[dict[str, Any]]:
    streets = (
        await db.execute(select(Street.name, Street.name_norm).order_by(Street.name_norm))
    ).all()
    dictionary = {row.name_norm: row for row in streets}
    mentions: dict[tuple[int, int], str] = {}
    normalized_text = text.lower().replace("ё", "е")
    for street in streets:
        for form in street_forms(street.name):
            for match in re.finditer(r"(?<!\w)" + re.escape(form) + r"(?!\w)", normalized_text):
                mentions[(match.start(), match.end())] = street.name_norm
    for match in MENTIONS.finditer(text):
        group = "before" if match.group("before") else "after"
        start, end = match.span(group)
        if not any(left <= start < right for left, right in mentions):
            mentions[(start, end)] = normalize_street(nominative(match.group(group)))
    expected_norm = expected_address.lower().replace("ё", "е")
    expected_trap = next(
        (
            name
            for name in sorted(TRAP_STREETS)
            if any(form in expected_norm for form in street_forms(name))
        ),
        None,
    )
    issues: list[dict[str, Any]] = []
    for (start, end), name in sorted(mentions.items()):
        # A correct explicit negation such as "не Дубининская" is not an address substitution.
        if re.search(r"\bне\s*$", normalized_text[max(0, start - 4) : start]):
            continue
        substituted = expected_trap and name in TRAP_STREETS and name != expected_trap
        if name in dictionary and not substituted:
            continue
        candidates = await street_candidates(db, name)
        suggestions = [street.name for street, _ in candidates]
        if substituted and expected_trap in dictionary:
            correct = dictionary[expected_trap].name
            suggestions = [correct, *(item for item in suggestions if item != correct)]
        kind = "ADDRESS_TYPO" if candidates else "UNKNOWN_STREET"
        issues.append(
            {
                "kind": kind,
                "severity": "CRITICAL" if kind == "ADDRESS_TYPO" else "MINOR",
                "offset": utf16_offset(text, start),
                "length": utf16_offset(text[start:end], end - start),
                "text": text[start:end],
                "suggestions": suggestions,
                "message": "Улица отличается от адреса карточки. Проверьте название."
                if substituted
                else "Возможно, название улицы написано с ошибкой."
                if candidates
                else "Улица не найдена в местном справочнике.",
            }
        )
    return issues
