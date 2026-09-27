import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from openpyxl import load_workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import (
    AuditLog,
    IncidentGroup,
    IncidentType,
    IncidentTypeService,
    ModifierService,
    Service,
)


def read_classifier(path: Path) -> dict[str, list[dict[str, Any]]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        for sheet in ("services", "incident_groups", "incident_types", "modifier_services"):
            rows = iter(workbook[sheet].values)
            headers = [str(value) for value in next(rows)]
            result[sheet] = [
                dict(zip(headers, values, strict=True))
                for values in rows
                if any(value is not None for value in values)
            ]
    finally:
        workbook.close()
    codes = {str(row["code"]) for row in result["services"]}
    groups = {str(row["code"]) for row in result["incident_groups"]}
    type_codes: set[str] = set()
    for row in result["incident_types"]:
        row["attributes"] = json.loads(row["attributes"])
        row["services"] = str(row["services"]).split(",")
        if (
            row["code"] in type_codes
            or row["group_code"] not in groups
            or not set(row["services"]) <= codes
        ):
            raise ValueError("Некорректная связь или повтор кода в классификаторе.")
        if not 1 <= int(row["difficulty"]) <= 10:
            raise ValueError("Сложность должна быть от 1 до 10.")
        type_codes.add(row["code"])
    return result


async def import_classifier(
    session: AsyncSession, path: Path, actor_id: UUID | None = None
) -> dict[str, int]:
    data = await asyncio.to_thread(read_classifier, path)
    services = {row.code: row for row in await session.scalars(select(Service))}
    for row in data["services"]:
        service = services.get(row["code"])
        if service is None:
            service = Service(code=row["code"])
            session.add(service)
            services[row["code"]] = service
        service.name, service.short_name = row["name"], row["short_name"]
        service.is_visible, service.sort_order = bool(row["is_visible"]), int(row["sort_order"])
    groups = {row.code: row for row in await session.scalars(select(IncidentGroup))}
    for row in data["incident_groups"]:
        group = groups.get(row["code"])
        if group is None:
            group = IncidentGroup(code=row["code"])
            session.add(group)
            groups[row["code"]] = group
        group.name, group.sort_order = row["name"], row["sort_order"]
    await session.flush()
    types = {row.code: row for row in await session.scalars(select(IncidentType))}
    for row in data["incident_types"]:
        incident = types.get(row["code"])
        if incident is None:
            incident = IncidentType(code=row["code"])
            session.add(incident)
        incident.group_id, incident.name = groups[row["group_code"]].id, row["name"]
        incident.attributes, incident.difficulty = row["attributes"], int(row["difficulty"])
        await session.flush()
        existing = set(
            await session.scalars(
                select(IncidentTypeService.service_id).where(
                    IncidentTypeService.incident_type_id == incident.id
                )
            )
        )
        wanted = {services[code].id for code in row["services"]}
        for service_id in wanted - existing:
            session.add(IncidentTypeService(incident_type_id=incident.id, service_id=service_id))
        await session.execute(
            delete(IncidentTypeService).where(
                IncidentTypeService.incident_type_id == incident.id,
                IncidentTypeService.service_id.in_(existing - wanted),
            )
        )
    for row in data["modifier_services"]:
        service_id = services[row["service_code"]].id
        if await session.get(ModifierService, (row["modifier"], service_id)) is None:
            session.add(ModifierService(modifier=row["modifier"], service_id=service_id))
    counts = {key: len(data[key]) for key in ("services", "incident_groups", "incident_types")}
    session.add(
        AuditLog(
            user_id=actor_id, action="CLASSIFIER_IMPORTED", entity_type="classifier", payload=counts
        )
    )
    await session.flush()
    return counts


async def main() -> None:
    async with session_factory.begin() as session:
        print(await import_classifier(session, Path("/data/classifier.xlsx")))


if __name__ == "__main__":
    asyncio.run(main())
