"""Create audited, separate demo-derived accounts for one browser acceptance run."""

import asyncio
import json
from uuid import uuid4

from sqlalchemy import select

from app.db.base import session_factory
from app.db.models import AuditLog, User


async def main() -> None:
    suffix = uuid4().hex[:12]
    logins = {}
    async with session_factory() as db, db.begin():
        for login in ("teacher", "student1", "admin"):
            original = (await db.scalars(select(User).where(User.login == login))).one()
            user = User(
                login=f"browser_{suffix}_{login}",
                password_hash=original.password_hash,
                role=original.role,
                service_id=original.service_id,
                first_name="Приёмка",
                last_name="Браузер",
            )
            db.add(user)
            await db.flush()
            db.add(
                AuditLog(
                    action="BROWSER_ACCEPTANCE_USER_CREATED", entity_type="users", entity_id=user.id
                )
            )
            logins[login] = user.login
    print(json.dumps(logins))


if __name__ == "__main__":
    asyncio.run(main())
