from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Scenario
from app.seeds.import_tickets import anonymize, import_tickets, parse_tickets


def test_table_parser_and_anonymization_of_ninety_authored_rows() -> None:
    # Synthetic parser coverage is not acceptance of missing customer PDFs.
    text = "№   Ситуация   Адрес\n" + "\n".join(
        f"{index}   Пожар в частном доме. Иванов Иван Иванович, +7 999 123-45-67."
        f"   ул. Дубнинская, д. {index}"
        for index in range(1, 91)
    )
    tickets = parse_tickets(text)
    assert len(tickets) == 90
    assert tickets[0].address == "ул. Дубнинская, д. 1"
    for ticket in tickets:
        cleaned = anonymize(ticket.description, ticket.number)
        assert "Иванов Иван Иванович" not in cleaned and "999 123-45-67" not in cleaned
        assert "Учебный заявитель" in cleaned and "+7 000" in cleaned
        assert anonymize(ticket.description, ticket.number) == cleaned


def test_parser_handles_pipe_separator_wrapping_and_rejects_non_rows() -> None:
    rows = parse_tickets(
        "№ | Ситуация | Адрес\n1 | Пожар в частном доме | ул. Дубнинская, д. 2\n"
        "Дополнительные сведения\n2 | Без адреса | отсутствует"
    )
    assert len(rows) == 1
    assert "Дополнительные сведения" in rows[0].description
    assert anonymize("Иванова И. С. позвонила", "1").startswith("Учебный заявитель")


async def test_ocr_import_is_anonymized_idempotent_and_keeps_duplicate_number_rows(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "tickets.pdf"
    source.write_bytes(b"synthetic fixture for mocked OCR output")
    text = "\n".join(
        f"{index if index != 75 else 15}   Пожар в частном доме. "
        f"Иванов Иван Иванович, +7 999 123-45-67.   ул. Дубнинская, д. {index}"
        for index in range(1, 91)
    )
    monkeypatch.setattr("app.seeds.import_tickets.ocr_pdf", lambda source: text)
    assert await import_tickets(db, source) == {
        "recognized": 90,
        "imported": 90,
        "rejected": 0,
        "duplicates": 0,
    }
    result = await import_tickets(db, source)
    assert result["imported"] == 0 and result["duplicates"] == 90
    rows = list(await db.scalars(select(Scenario).where(Scenario.source == "TICKET")))
    assert len(rows) == 90
    assert len({row.card_payload["address"]["raw"] for row in rows}) == 90
    for row in rows:
        assert "Иванов Иван Иванович" not in str(row.card_payload)
        assert "999 123-45-67" not in str(row.card_payload)
        assert row.status == "PENDING_REVIEW"
