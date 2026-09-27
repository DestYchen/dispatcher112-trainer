from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error

from app.api.errors import APIError


def page_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = int(urlsafe_b64decode(cursor.encode()).decode())
        if value < 0 or value > 10000000:
            raise ValueError("cursor range")
        return value
    except (ValueError, Error, UnicodeError) as error:
        raise APIError(400, "VALIDATION_ERROR", "Некорректный указатель страницы.") from error


def next_cursor(offset: int, limit: int, has_more: bool) -> str | None:
    return urlsafe_b64encode(str(offset + limit).encode()).decode() if has_more else None
