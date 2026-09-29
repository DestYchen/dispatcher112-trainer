"""Authored fixtures for lesson delivery before the generation stage."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import AuditLog, IncidentType, Scenario, User
from app.domain.classifier import resolve_services


async def manual_scenarios(db: AsyncSession) -> None:
    teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
    incident = (await db.scalars(select(IncidentType).where(IncidentType.code.in_(("01.01.01", "1050901"))).order_by(IncidentType.code).limit(1))).one()
    fixtures = [
        (
            "Учебный пожар: доклад дежурному",
            "OPERATOR_112",
            "Дубнинская улица, д. 28",
            "Пожар в частном доме. Люди эвакуированы, пострадавших нет. "
            "Сообщите оперативному дежурному адрес и принятые меры.",
            None,
        ),
        (
            "Учебный пожар: двор",
            "OPERATOR_112",
            "Дубнинская улица, д. 28",
            "Горит крыша частного дома во дворе. Люди вышли из дома, пострадавших нет. "
            "Дом не газифицирован.",
            None,
        ),
        (
            "Учебный пожар: внешняя система",
            "EXTERNAL_SYSTEM",
            "Чертановская улица, д. 9",
            "Поступило сообщение о пожаре в частном доме. Подъезд через двор свободен. "
            "Люди эвакуированы, пострадавших нет.",
            "DETAIL_IN_DESCRIPTION",
        ),
        (
            "Учебный пожар: уточнение адреса",
            "OPERATOR_112",
            "Дубнинская улица, д. 12",
            "Пожар в частном доме. Заявитель уточнил: Дубнинская улица, не Дубининская. "
            "Люди на улице, пострадавших нет.",
            "AMBIGUOUS_ADDRESS",
        ),
    ]
    for title, origin, address, description, trap in fixtures:
        if await db.scalar(
            select(Scenario.id).where(Scenario.title == title, Scenario.source == "MANUAL")
        ):
            continue
        now = datetime.now(UTC)
        services = await resolve_services(db, incident.id, [])
        scenario = Scenario(
            title=title,
            source="MANUAL",
            status="APPROVED",
            origin=origin,
            incident_type_id=incident.id,
            difficulty=3,
            author_id=teacher.id,
            approved_by=teacher.id,
            approved_at=now,
            card_payload={
                "registered_at": now.isoformat(),
                "operator_workstation": "ОП-034" if origin == "OPERATOR_112" else None,
                "applicant": {"name": "Учебный заявитель", "phone": "+7 900 ***-**-71"},
                "address": {
                    "raw": address,
                    "clarification": "Москва, учебный адрес",
                    "lat": 55.863,
                    "lon": 37.558,
                },
                "attributes": list(incident.attributes.values())
                if origin == "OPERATOR_112"
                else [],
                "incident_type_name": incident.name,
                "modifiers": [],
                "description": description,
                "notified_services": [service.code for service in services],
            },
            reference={
                "expected_status": "ACCEPTED",
                "expected_status_chain": [
                    "ACCEPTED",
                    "RESPONSE_STARTED",
                    "ARRIVED",
                    "WORK_IN_PROGRESS",
                    "WORK_COMPLETED",
                ],
                "comment_required": False,
                "comment_must_contain": [],
                "report_required": title.endswith("доклад дежурному"),
                "report_callee_code": "DUTY_OFFICER"
                if title.endswith("доклад дежурному")
                else None,
                "report_must_mention": ["address", "incident_type", "victims"]
                if title.endswith("доклад дежурному")
                else [],
                "rationale": "Пожар на территории района требует реагирования ДДС.",
                "trap": trap,
            },
        )
        db.add(scenario)
        await db.flush()
        db.add(
            AuditLog(
                user_id=teacher.id,
                action="MANUAL_FIXTURE_IMPORTED",
                entity_type="scenario",
                entity_id=scenario.id,
            )
        )


async def main() -> None:
    async with session_factory() as db, db.begin():
        await manual_scenarios(db)


if __name__ == "__main__":
    asyncio.run(main())
