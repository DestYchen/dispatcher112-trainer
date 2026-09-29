from pathlib import Path

import pytest
from argon2 import PasswordHasher
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, IncidentGroup, IncidentType, Service, Street, User
from app.domain.classifier import (
    normalize_street,
    resolve_services,
    resolve_type,
    street_candidates,
)
from app.seeds.create_demo_users import create_demo_users
from app.seeds.import_classifier import import_classifier, read_classifier
from app.seeds.import_streets import import_streets


def test_real_customer_classifier_parses() -> None:
    # data/classifier.xlsx is converted from the customer's ЕКП v046_24 (scripts/convert_real_classifier.py).
    # Parsed only: the shared test database keeps the synthetic fixture the other tests rely on.
    data = read_classifier(Path("/data/classifier.xlsx"))
    assert (len(data["services"]), len(data["incident_groups"]), len(data["incident_types"])) == (230, 23, 1131)
    fire = next(row for row in data["incident_types"] if row["code"] == "1050901")
    assert fire["name"] == "пожар: частный дом"
    assert fire["attributes"] == {"level1": "жилой дом", "level2": "частный дом", "level3": "открытое пламя"}
    assert "S101" in fire["services"]


async def test_import_counts_and_idempotence(db: AsyncSession) -> None:
    for _ in range(2):
        counts = await import_classifier(db, Path("/data/classifier-synthetic.xlsx"))
        await import_streets(db, Path("/data/streets.csv"))
        await create_demo_users(db)
        assert counts == {"services": 58, "incident_groups": 24, "incident_types": 1200}
        for model, count in [
            (Service, 58),
            (IncidentGroup, 24),
            (IncidentType, 1200),
            (Street, 50),
            (User, 3),
        ]:
            assert await db.scalar(select(func.count()).select_from(model)) == count


async def test_fire_routing_and_modifiers(db: AsyncSession) -> None:
    incident = await resolve_type(
        db, {"level1": "на улице", "level2": "частный дом", "level3": "открытое пламя"}
    )
    assert incident.name == "пожар: частный дом"
    base = {s.code for s in await resolve_services(db, incident.id, [])}
    assert base == {"MCHS", "MVD", "DDS_CHERTANOVO"}
    extended = [
        s.code for s in await resolve_services(db, incident.id, ["THREAT_TO_PEOPLE", "VICTIMS"])
    ]
    assert set(extended) == base | {"SMP"}
    assert len(extended) == len(set(extended))
    assert "INFORMATION" not in extended


async def test_invalid_attributes_rejected(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await resolve_type(db, {"level1": "неизвестно"})


async def test_real_postgres_trigrams(db: AsyncSession) -> None:
    results = await street_candidates(db, "Дубининская")
    assert any(street.name == "Дубнинская улица" and score > 0.7 for street, score in results)
    assert results[0][0].name == "Дубининская улица"
    assert normalize_street(" ул. Дубнинская ") == "дубнинская улица"
    assert normalize_street("Ёлочная") == "елочная улица"


async def test_seed_passwords_are_argon2id_and_not_reset(db: AsyncSession) -> None:
    user = (await db.scalars(select(User).where(User.login == "student1"))).one()
    assert user.password_hash.startswith("$argon2id$")
    hasher = PasswordHasher()
    assert hasher.verify(user.password_hash, "student")
    user.password_hash = hasher.hash("changed")
    await db.flush()
    await create_demo_users(db)
    assert hasher.verify(user.password_hash, "changed")


async def test_audit_is_append_only(db: AsyncSession) -> None:
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(update(AuditLog).values(action="REWRITTEN"))
