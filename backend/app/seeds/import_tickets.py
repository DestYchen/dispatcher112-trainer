"""Offline PDF OCR and anonymized import; originals never enter student payloads."""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import AuditLog, IncidentType, Scenario, Street, User
from app.domain.classifier import normalize_street, resolve_services
from app.generation.builder import build_reference


@dataclass(frozen=True)
class Ticket:
    number: str
    description: str
    address: str


def anonymize(text: str, seed: str) -> str:
    number = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    name = ["Учебный заявитель Анна", "Учебный заявитель Иван", "Учебный заявитель Мария"][
        number % 3
    ]
    text = re.sub(r"(?:\+7|8)[\s(\d)-]{9,}", f"+7 000 ***-**-{number % 100:02d}", text)
    text = re.sub(
        r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\b|\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.",
        name,
        text,
    )
    return text


def parse_tickets(text: str) -> list[Ticket]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if re.search(r"Ситуация\s+Адрес|№\s*[|]?\s*Ситуация", line, re.I):
            continue
        match = re.match(r"^\s*(\d{1,4})[.)]?\s+\|?\s*(.+)", line)
        if match:
            current = None
            contents = match[2]
            columns = [
                item.strip() for item in re.split(r"\s*\|\s*|\s{2,}", contents) if item.strip()
            ]
            address_index = next(
                (
                    index
                    for index, item in enumerate(columns)
                    if index > 0
                    and re.search(r"улица|ул\.|шоссе|проспект|переулок|бульвар|проезд", item, re.I)
                ),
                None,
            )
            if address_index is not None:
                description = " ".join(columns[:address_index])
                address = " ".join(columns[address_index:])
                address_column = line.find(columns[address_index])
            else:
                found = re.search(
                    r"(?:ул\.|улица|шоссе|проспект|переулок|бульвар|проезд)\s+[А-ЯЁа-яё].*",
                    contents,
                    re.I,
                )
                if found is None or found.start() == 0:
                    continue
                description, address = contents[: found.start()].strip(), found.group().strip()
                address_column = line.find(address)
            current = {
                "number": match[1],
                "description": description,
                "address": address,
                "address_column": address_column,
            }
            rows.append(current)
        elif current and line.strip():
            boundary = current["address_column"]
            if line.startswith(" ") and len(line) > boundary:
                left, right = line[:boundary].strip(), line[boundary:].strip()
                current["description"] += " " + left if left else ""
                current["address"] += " " + right if right else ""
            else:
                current["description"] += " " + line.strip()
    return [
        Ticket(str(row["number"]), str(row["description"]), str(row["address"]))
        for row in rows
        if len(row["description"]) >= 10 and row["address"]
    ]


def ocr_pdf(source: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="dispatcher-ocr-") as temporary:
        output = Path(temporary) / "recognized.pdf"
        extracted = Path(temporary) / "recognized.txt"
        subprocess.run(
            [
                "ocrmypdf",
                "--force-ocr",
                "--language",
                "rus",
                "--tesseract-pagesegmode",
                "6",
                "--jobs",
                "2",
                "--output-type",
                "pdf",
                str(source.resolve()),
                str(output),
            ],
            check=True,
            timeout=1800,
            capture_output=True,
        )
        subprocess.run(
            ["pdftotext", "-layout", str(output), str(extracted)],
            check=True,
            timeout=60,
            capture_output=True,
        )
        return extracted.read_text(encoding="utf-8")


def words(text: str) -> set[str]:
    return {word[:5] for word in re.findall(r"[а-яё]{4,}", text.lower().replace("ё", "е"))}


async def import_tickets(db: AsyncSession, source: Path) -> dict[str, int]:
    text = await asyncio.to_thread(ocr_pdf, source)
    tickets = parse_tickets(text)
    if not tickets:
        raise ValueError("В PDF не найдены строки таблицы «№ / Ситуация / Адрес».")
    types = list(await db.scalars(select(IncidentType).order_by(IncidentType.code)))
    streets = list(await db.scalars(select(Street).order_by(Street.name_norm)))
    teacher = await db.scalar(select(User).where(User.login == "teacher"))
    if teacher is None:
        raise ValueError("Сначала создайте учебного преподавателя командой make seed.")
    imported = rejected = duplicates = 0
    digest = hashlib.sha256(await asyncio.to_thread(source.read_bytes)).hexdigest()
    for position, ticket in enumerate(tickets, start=1):
        # OCR can confuse 75 and 15. Physical rows identify distinct tickets.
        key = f"{digest}:row:{position}"
        exists = await db.scalar(
            select(Scenario.id).where(Scenario.card_payload["ticket_key"].astext == key)
        )
        if exists:
            duplicates += 1
            continue
        clean_address = anonymize(ticket.address, key)
        address_norm = normalize_street(clean_address.split(",")[0])
        street = next((row for row in streets if row.name_norm == address_norm), None)
        description = anonymize(ticket.description, key)
        description_words = words(description)
        ranked = sorted(
            types, key=lambda item: (-len(words(item.name) & description_words), item.code)
        )
        if not ranked or street is None or not (words(ranked[0].name) & description_words):
            rejected += 1
            continue
        incident = ranked[0]
        services = await resolve_services(db, incident.id, [])
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "ticket_key": key,
            "registered_at": now.isoformat(),
            "operator_workstation": None,
            "applicant": {"name": "Учебный заявитель", "phone": "+7 000 ***-**-00"},
            "address": {
                "raw": clean_address,
                "clarification": "Адрес из учебного билета",
                "lat": None,
                "lon": None,
            },
            "attributes": [],
            "modifiers": [],
            "incident_type_name": incident.name,
            "description": description,
            "notified_services": [row.code for row in services],
            "validation": [
                {
                    "code": "OCR_REVIEW",
                    "ok": False,
                    "action": "REVIEW",
                    "message": "Сверьте распознанный текст и выбранный тип с учебным билетом.",
                }
            ],
        }
        row = Scenario(
            title=f"Билет {ticket.number}: {incident.name}",
            source="TICKET",
            status="PENDING_REVIEW",
            origin="EXTERNAL_SYSTEM",
            incident_type_id=incident.id,
            difficulty=incident.difficulty,
            author_id=teacher.id,
            card_payload=payload,
            reference=build_reference(payload["notified_services"], "DDS_DISTRICT" if "DDS_DISTRICT" in payload["notified_services"] else "DDS_CHERTANOVO"),
        )
        db.add(row)
        await db.flush()
        db.add(
            AuditLog(
                user_id=teacher.id,
                action="TICKET_IMPORTED",
                entity_type="scenario",
                entity_id=row.id,
                payload={"source_sha256": digest, "ticket_number": ticket.number},
            )
        )
        imported += 1
    return {
        "recognized": len(tickets),
        "imported": imported,
        "rejected": rejected,
        "duplicates": duplicates,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Локальное распознавание учебных билетов")
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    async with session_factory() as db, db.begin():
        result = await import_tickets(db, args.source)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
