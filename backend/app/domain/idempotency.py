import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import APIError
from app.db.models import RequestReceipt


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def previous_response(
    db: AsyncSession,
    user_id: UUID,
    assignment_id: UUID,
    kind: str,
    key: str,
    body: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    receipt = await db.get(RequestReceipt, (user_id, assignment_id, kind, key))
    if receipt is None:
        return None
    if receipt.expires_at <= now:
        await db.delete(receipt)
        await db.flush()
        return None
    if receipt.request_hash != body_hash(body):
        raise APIError(
            400, "VALIDATION_ERROR", "Ключ повтора уже использован для другого действия."
        )
    return receipt.response


def save_response(
    db: AsyncSession,
    user_id: UUID,
    assignment_id: UUID,
    kind: str,
    key: str,
    body: dict[str, Any],
    response: dict[str, Any],
    now: datetime,
) -> None:
    db.add(
        RequestReceipt(
            user_id=user_id,
            assignment_id=assignment_id,
            kind=kind,
            key=key,
            request_hash=body_hash(body),
            response=response,
            expires_at=now + timedelta(minutes=10),
        )
    )
