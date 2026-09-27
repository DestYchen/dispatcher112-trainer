from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, User
from app.operations import recovery_switch as switching
from app.operations.update_database import save_setting, setting


@pytest.fixture
def switch_db(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        yield db

    monkeypatch.setattr(switching, "session_factory", session)


async def request(db: AsyncSession, **changes: Any) -> dict[str, Any]:
    actor = await db.scalar(select(User.id).where(User.login == "admin"))
    return {
        "id": str(uuid4()),
        "actor": str(actor),
        "action": "fence",
        "reason": "Проверка аварийного переключения",
        **changes,
    }


async def test_switch_preserves_both_audit_branches_and_import_is_idempotent(
    db: AsyncSession, switch_db: None
) -> None:
    value = await request(db)
    previous = {"enabled": False, "reason": "До переключения"}
    await save_setting(db, "maintenance", previous, UUID(value["actor"]))
    first = await switching.command(value)
    assert await switching.command(value) == first
    assert (await setting(db, "maintenance"))["switch_id"] == value["id"]
    history = await switching.command({**value, "action": "export", "source": "primary"})
    # The recovered branch has an event with the same numeric ID and different content.
    original = dict(history["rows"][-1])
    conflicting = {**original, "action": "RECOVERED_BRANCH_ACTION", "payload": {"kept": True}}
    incoming = {"source": "restored", "rows": [original, conflicting, conflicting]}
    result = await switching.command({**value, "action": "import", "history": incoming})
    assert result["imported"] == 1
    assert (await switching.command({**value, "action": "import", "history": incoming}))[
        "imported"
    ] == 1
    saved = await db.scalar(select(AuditLog).where(AuditLog.id == original["id"]))
    assert saved is not None and saved.action == original["action"]
    imported = (
        await db.scalars(select(AuditLog).where(AuditLog.action == "RECOVERY_AUDIT_IMPORTED"))
    ).all()
    assert len(imported) == 1
    assert imported[0].payload is not None
    assert imported[0].payload["original"] == conflicting
    # A return transfer unwraps prior imports instead of nesting and duplicating them.
    return_history = await switching.command({**value, "action": "export", "source": "restored"})
    assert (await switching.command({**value, "action": "import", "history": return_history}))[
        "imported"
    ] == 1
    released = await switching.command({**value, "action": "release", "phase": "ACTIVE"})
    assert await setting(db, "maintenance") == previous
    assert await switching.command({**value, "action": "release", "phase": "ACTIVE"}) == released


@pytest.mark.parametrize("owner", ["job_id", "update_id", "switch_id", "topology_id"])
async def test_switch_cannot_take_another_operation_maintenance(
    db: AsyncSession, switch_db: None, owner: str
) -> None:
    value = await request(db)
    previous = {"enabled": True, owner: str(uuid4())}
    await save_setting(db, "maintenance", previous, UUID(value["actor"]))
    with pytest.raises(ValueError, match="Another maintenance"):
        await switching.command(value)
    assert await setting(db, "maintenance") == previous


@pytest.mark.parametrize("action", ["fence", "export", "import", "release"])
@pytest.mark.parametrize("login", ["teacher", "student1", "inactive", "missing"])
async def test_switch_commands_require_active_administrator(
    db: AsyncSession, switch_db: None, action: str, login: str
) -> None:
    value = await request(db, action=action)
    if login == "missing":
        value["actor"] = str(uuid4())
    else:
        user = await db.scalar(
            select(User).where(User.login == ("admin" if login == "inactive" else login))
        )
        assert user is not None
        if login == "inactive":
            user.is_active = False
            await db.flush()
        value["actor"] = str(user.id)
    with pytest.raises(ValueError, match="active administrator"):
        await switching.command(value)


@pytest.mark.parametrize("action", ["export", "import", "release"])
async def test_history_exchange_requires_ownership(
    db: AsyncSession, switch_db: None, action: str
) -> None:
    value = await request(db)
    await switching.command(value)
    with pytest.raises(ValueError, match="does not own"):
        await switching.command({**value, "id": str(uuid4()), "action": action, "phase": "ACTIVE"})


@pytest.mark.parametrize("reason", ["", "    ", "1234", "x" * 1001])
async def test_switch_requires_meaningful_bounded_reason(
    db: AsyncSession, switch_db: None, reason: str
) -> None:
    with pytest.raises(ValueError, match="reason"):
        await switching.command(await request(db, reason=reason))


async def test_export_limits_preserve_original_database(
    db: AsyncSession, switch_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await request(db)
    await switching.command(value)
    monkeypatch.setattr(switching, "MAX_TAIL_ROWS", 0)
    with pytest.raises(ValueError, match="limit exceeded"):
        await switching.command({**value, "action": "export", "source": "primary"})
    assert (await setting(db, "maintenance"))["switch_id"] == value["id"]
    assert await db.scalar(select(AuditLog.id).where(AuditLog.action == "RECOVERY_SWITCH_FENCE"))
