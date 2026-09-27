import re
from uuid import UUID

from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IncidentType, IncidentTypeService, ModifierService, Service, Street


def normalize_street(name: str) -> str:
    name = re.sub(r"\s+", " ", name.lower().replace("ё", "е").strip())
    name = re.sub(r"^ул(?:ица|\.)?\s+", "", name)
    name = re.sub(r"\s+ул(?:ица|\.)?$", "", name)
    if not any(
        word in name
        for word in ("шоссе", "проспект", "переулок", "бульвар", "площадь", "набережная", "проезд")
    ):
        name += " улица"
    return name


async def resolve_type(session: AsyncSession, attributes: dict[str, str]) -> IncidentType:
    found = list(
        await session.scalars(select(IncidentType).where(IncidentType.attributes == attributes))
    )
    if len(found) != 1:
        raise ValueError("Признаки должны однозначно определять тип происшествия.")
    return found[0]


async def resolve_services(
    session: AsyncSession, type_id: UUID, modifiers: list[str]
) -> list[Service]:
    ids = union(
        select(IncidentTypeService.service_id).where(
            IncidentTypeService.incident_type_id == type_id
        ),
        select(ModifierService.service_id).where(ModifierService.modifier.in_(modifiers)),
    )
    return list(
        await session.scalars(
            select(Service)
            .where(Service.id.in_(ids), Service.is_visible.is_(True))
            .order_by(Service.sort_order, Service.code)
        )
    )


async def street_candidates(session: AsyncSession, name: str) -> list[tuple[Street, float]]:
    similarity = func.similarity(Street.name_norm, normalize_street(name))
    rows = await session.execute(
        select(Street, similarity)
        .where(similarity > 0.7)
        .order_by(similarity.desc(), Street.name_norm)
    )
    return [(street, float(score)) for street, score in rows]
