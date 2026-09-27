import asyncio
import csv
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import AuditLog, Street
from app.domain.classifier import normalize_street


def read_streets(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows or any(
        not row.get("name", "").strip()
        or len(row["name"]) > 255
        or len(row.get("district", "") or "") > 128
        for row in rows
    ):
        raise ValueError("Некорректная строка справочника улиц.")
    return rows


async def import_streets(session: AsyncSession, path: Path, actor_id: UUID | None = None) -> int:
    existing = {row.name_norm: row for row in await session.scalars(select(Street))}
    rows = await asyncio.to_thread(read_streets, path)
    for row in rows:
        norm = normalize_street(row["name"])
        street = existing.get(norm)
        if street is None:
            street = Street(name_norm=norm)
            session.add(street)
            existing[norm] = street
        street.name, street.district = row["name"], row.get("district")
    session.add(
        AuditLog(
            user_id=actor_id,
            action="STREETS_IMPORTED",
            entity_type="streets",
            payload={"count": len(rows)},
        )
    )
    await session.flush()
    return len(rows)


async def main() -> None:
    async with session_factory.begin() as session:
        print({"streets": await import_streets(session, Path("/data/streets.csv"))})


if __name__ == "__main__":
    asyncio.run(main())
