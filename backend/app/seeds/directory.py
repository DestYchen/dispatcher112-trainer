import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import DirectoryEntry

ENTRIES = [
    {
        "code": "DUTY_OFFICER",
        "number": "2201",
        "title": "Оперативный дежурный",
        "voice": "male_calm",
        "greeting": "Слушаю вас.",
        "confirmation": "Я вас понял, информация принята.",
    },
    {
        "code": "HEAD_ENGINEER",
        "number": "2214",
        "title": "Главный инженер",
        "voice": "male_brisk",
        "greeting": "Главный инженер. Докладывайте.",
        "confirmation": "Принято. Приступаю к организации работ.",
    },
    {
        "code": "SYSTEM_112",
        "number": "112",
        "title": "Служба 112",
        "voice": "female_calm",
        "greeting": "Служба сто двенадцать. Слушаю вас.",
        "confirmation": "Информация принята и зарегистрирована.",
    },
    {
        "code": "SHIFT_SUPERVISOR",
        "number": "2210",
        "title": "Начальник смены",
        "voice": "female_brisk",
        "greeting": "Начальник смены. Докладывайте обстановку.",
        "confirmation": "Я вас поняла. Действуйте по обстановке.",
    },
]


async def seed_directory(db: AsyncSession) -> None:
    for entry in ENTRIES:
        row = await db.scalar(select(DirectoryEntry).where(DirectoryEntry.code == entry["code"]))
        if row is None:
            db.add(DirectoryEntry(**entry))
    await db.flush()


async def main() -> None:
    async with session_factory() as db, db.begin():
        await seed_directory(db)
    print("Телефонный справочник: 4 абонента")


if __name__ == "__main__":
    asyncio.run(main())
