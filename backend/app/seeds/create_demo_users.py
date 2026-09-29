import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import AuditLog, Service, User, Workstation
from app.domain.security import password_hasher


async def create_demo_users(session: AsyncSession) -> None:
    service = (await session.scalars(select(Service).where(Service.code.in_(("DDS_CHERTANOVO", "DDS_DISTRICT"))).order_by(Service.code).limit(1))).one()
    for login, password, role, last_name in [
        ("admin", "admin", "ADMIN", "Администратор"),
        ("teacher", "teacher", "TEACHER", "Преподаватель"),
        ("student1", "student", "STUDENT", "Иванов"),
    ]:
        if await session.scalar(select(User).where(User.login == login)) is None:
            user = User(
                login=login,
                password_hash=await asyncio.to_thread(password_hasher.hash, password),
                role=role,
                last_name=last_name,
                first_name="Иван",
                service_id=service.id if role == "STUDENT" else None,
            )
            session.add(user)
            await session.flush()
            session.add(
                AuditLog(
                    action="USER_CREATED",
                    entity_type="users",
                    entity_id=user.id,
                    payload={"login": login, "source": "seed"},
                )
            )
    if await session.scalar(select(Workstation).where(Workstation.number == "АРМ-07")) is None:
        workstation = Workstation(number="АРМ-07", room="Учебный класс")
        session.add(workstation)
        await session.flush()
        session.add(
            AuditLog(
                action="WORKSTATION_CREATED", entity_type="workstations", entity_id=workstation.id
            )
        )


async def main() -> None:
    async with session_factory.begin() as session:
        await create_demo_users(session)
    print("Учебные пользователи созданы; существующие пароли сохранены.")


if __name__ == "__main__":
    asyncio.run(main())
