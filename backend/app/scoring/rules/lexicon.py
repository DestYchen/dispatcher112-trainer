import re

REASON = re.compile(
    r"не обслуж|обслуживает|не относ|вне зон|друг(?:ая|ой|ую)|дубл|повтор|ошиб|"
    r"профил|принадлеж|причин|ответствен",
    re.I,
)
HANDED = re.compile(r"переда|направл|уведом|сообщ|оповещ|связал", re.I)
CARD_NUMBER = re.compile(r"\b\d{4}-\d{4}-\d{6}\b")
ADDRESS = re.compile(
    r"(?:ул(?:ица|ице|ицу|\.)?|шоссе|проспект|переулок|проезд|бульвар).{0,70}\d+", re.I
)
EXTERNAL_ORGANIZATIONS = (
    "ПИК",
    "Жилищник",
    "ДЕЗ",
    "РЭУ",
    "Мосгаз",
    "Мосводоканал",
    "МЧС",
    "МВД",
    "СМП",
    "112",
)


def normalized(text: str) -> str:
    return text.lower().replace("ё", "е")


def has_organization(text: str, services: tuple[str, ...]) -> bool:
    text = normalized(text)
    return any(
        re.search(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", text)
        for name in (*services, *EXTERNAL_ORGANIZATIONS)
        if name.strip()
    )


def satisfies(requirement: str, text: str, services: tuple[str, ...]) -> bool:
    if requirement == "reason":
        return bool(REASON.search(text))
    if requirement == "handed_to":
        return bool(HANDED.search(text)) and has_organization(text, services)
    if requirement == "card_number":
        return bool(CARD_NUMBER.search(text))
    if requirement == "clarified_address":
        return bool(ADDRESS.search(text))
    return False
