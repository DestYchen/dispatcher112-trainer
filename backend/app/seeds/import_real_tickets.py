"""Load the customer's 32 exam tickets (96 caller situations) as approved scenarios.

Source: data/tickets/tickets.json, a verified transcription of «Билеты- задачи по С 112».
Each situation is mapped by hand to a type of the real classifier (TYPE_BY_TICKET). The
trainee plays the dispatcher of a service the card was sent to (the customer: «диспетчер
профильной ДДС»): a Moscow card is accepted, walked through the statuses and reported to the
duty officer; a card outside Moscow is «Не принята» with a comment saying where the information
was handed over.
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_factory
from app.db.models import AuditLog, IncidentType, Scenario, User
from app.domain.classifier import resolve_services

FULL_CHAIN = ["ACCEPTED", "RESPONSE_STARTED", "ARRIVED", "WORK_IN_PROGRESS", "WORK_COMPLETED"]

# "ticket.row" -> real classifier code (data/classifier.xlsx, converted from ЕКП v046_24).
TYPE_BY_TICKET = {
    "1.1": "1010101", "1.2": "15060202", "1.3": "22530000",
    "2.1": "1050602", "2.2": "15110700", "2.3": "2021400",
    "3.1": "1050901", "3.2": "15100100", "3.3": "2021700",
    "4.1": "1050201", "4.2": "22020000", "4.3": "17080100",
    "5.1": "1050101", "5.2": "22340000", "5.3": "17040100",
    "6.1": "1020201", "6.2": "22360000", "6.3": "17070100",
    "7.1": "1020201", "7.2": "22360000", "7.3": "17020202",
    "8.1": "1060402", "8.2": "22020000", "8.3": "17070200",
    "9.1": "1030202", "9.2": "22460000", "9.3": "17040600",
    "10.1": "1061601", "10.2": "22360000", "10.3": "17080100",
    "11.1": "1020101", "11.2": "22020000", "11.3": "17020203",
    "12.1": "1060402", "12.2": "22370000", "12.3": "17041200",
    "13.1": "1010401", "13.2": "22460000", "13.3": "22230000",
    "14.1": "1010201", "14.2": "22360000", "14.3": "17030101",
    "15.1": "1010501", "15.2": "22530000", "15.3": "17010500",
    "16.1": "1061601", "16.2": "22530000", "16.3": "17100200",
    "17.1": "1051600", "17.2": "22360000", "17.3": "22530000",
    "18.1": "1061601", "18.2": "22020000", "18.3": "17010800",
    "19.1": "1010201", "19.2": "21010200", "19.3": "17050300",
    "20.1": "15060201", "20.2": "22350000", "20.3": "17010700",
    "21.1": "15060202", "21.2": "22230000", "21.3": "17080600",
    "22.1": "15060100", "22.2": "22360000", "22.3": "17100100",
    "23.1": "15190000", "23.2": "22230000", "23.3": "18070000",
    "24.1": "15190000", "24.2": "22530000", "24.3": "15220400",
    "25.1": "15220100", "25.2": "2010100", "25.3": "19010000",
    "26.1": "15220600", "26.2": "2010100", "26.3": "19010101",
    "27.1": "15130700", "27.2": "2010100", "27.3": "6060100",
    "28.1": "15140000", "28.2": "2010100", "28.3": "14090404",
    "29.1": "15130600", "29.2": "2010600", "29.3": "4150000",
    "30.1": "15210102", "30.2": "2020100", "30.3": "13020300",
    "31.1": "15210101", "31.2": "2020100", "31.3": "13020201",
    "32.1": "15050802", "32.2": "2020900", "32.3": "14030203",
}

VICTIM_WORDS = re.compile(
    r"травм|кровотеч|ожог|без сознания|потер\w* сознани|ранен|пострадал(?!ших\s+(люд\w+\s+)?нет)|"
    r"в крови|избит|судорог|рожает|отошли воды|укусила|сбила|труп",
    re.IGNORECASE,
)
THREAT_WORDS = re.compile(r"кричат о помощи|кричит|тонет|угроз|заблокирован|на льдине", re.IGNORECASE)
CLARIFICATION = re.compile(r"\(\s*при уточнении[^)]*\)", re.IGNORECASE)


def split_address(raw: str) -> tuple[str, str | None]:
    """What the caller says first vs. what they reveal only when the dispatcher asks."""
    match = CLARIFICATION.search(raw)
    if not match:
        return raw.strip(), None
    said = (raw[: match.start()] + raw[match.end():]).strip(" ,.")
    revealed = re.sub(r"^\(\s*при уточнении( адреса)?\s*[-–—:]?\s*|\)$", "", match.group(0)).strip()
    return said, revealed


def modifiers_for(record: dict[str, Any]) -> list[str]:
    text = record["situation"]
    result = []
    if VICTIM_WORDS.search(text):
        result.append("VICTIMS")
    if THREAT_WORDS.search(text):
        result.append("THREAT_TO_PEOPLE")
    if "child_involved" in record.get("traps", []):
        result.append("CHILD_INVOLVED")
    return result


def scenario_difficulty(base: int, traps: list[str], revealed: str | None) -> int:
    # Prior only; Elo on real attempts takes over once the ticket has been played.
    extra = sum(t in traps for t in ("outside_moscow", "landmark_only_address", "caller_hung_up_or_no_contact"))
    return max(1, min(10, round(base * 0.6) + extra + (1 if revealed else 0)))


def build(record: dict[str, Any], incident: IncidentType, service_codes: list[str]) -> dict[str, Any]:
    traps = record.get("traps", [])
    said, revealed = split_address(record["address_raw"])
    outside = "outside_moscow" in traps
    own = not outside
    trap = "NOT_OUR_SERVICE" if outside else "AMBIGUOUS_ADDRESS" if revealed else (
        "DETAIL_IN_DESCRIPTION" if "landmark_only_address" in traps else None
    )
    rationale = (
        "Место происшествия за пределами Москвы: «Не принята», в комментарии — причина и куда передано "
        "(112 соответствующего региона)."
        if outside
        else "Профильное происшествие: принять в течение 30 секунд, провести по статусам, доложить дежурному."
    )
    return {
        "expected_status": "ACCEPTED" if own else "NOT_ACCEPTED",
        "expected_status_chain": FULL_CHAIN if own else ["NOT_ACCEPTED"],
        "comment_required": not own,
        "comment_must_contain": [] if own else ["reason", "handed_to"],
        "report_required": own,
        "report_callee_code": "DUTY_OFFICER" if own else None,
        "report_must_mention": ["address", "incident_type", "victims"] if own else [],
        "rationale": rationale,
        "trap": trap,
        "ticket": f"{record['ticket']}.{record['row']}",
        "ticket_traps": traps,
        "address_revealed_on_clarification": revealed,
        "address_as_said": said,
    }


async def import_real_tickets(db: AsyncSession, path: Path) -> int:
    teacher = (await db.scalars(select(User).where(User.login == "teacher"))).one()
    records = json.loads(path.read_text(encoding="utf-8"))
    types = {t.code: t for t in await db.scalars(select(IncidentType).where(IncidentType.code.in_(set(TYPE_BY_TICKET.values()))))}
    missing = sorted(set(TYPE_BY_TICKET.values()) - set(types))
    if missing:
        raise ValueError(f"В классификаторе нет типов: {missing}. Сначала импортируйте реальный классификатор.")
    created = 0
    for record in records:
        key = f"{record['ticket']}.{record['row']}"
        title = f"Билет {key}: {record['situation'][:80]}"
        if await db.scalar(select(Scenario.id).where(Scenario.title == title, Scenario.source == "TICKET")):
            continue
        incident = types[TYPE_BY_TICKET[key]]
        modifiers = modifiers_for(record)
        services = [s.code for s in await resolve_services(db, incident.id, modifiers)]
        reference = build(record, incident, services)
        said, revealed = reference["address_as_said"], reference["address_revealed_on_clarification"]
        now = datetime.now(UTC)
        scenario = Scenario(
            title=title[:255],
            source="TICKET",
            status="APPROVED",
            origin="OPERATOR_112",
            incident_type_id=incident.id,
            difficulty=scenario_difficulty(incident.difficulty, record.get("traps", []), revealed),
            author_id=teacher.id,
            approved_by=teacher.id,
            approved_at=now,
            card_payload={
                "registered_at": now.isoformat(),
                "operator_workstation": "ОП-034",
                "applicant": {"name": record.get("caller_name") or "Не представился",
                              "phone": record.get("caller_phone") or ""},
                "address": {"raw": said, "clarification": revealed or "", "lat": None, "lon": None},
                "attributes": list(incident.attributes.values()),
                "incident_type_name": incident.name,
                "modifiers": modifiers,
                "description": record["situation"],
                "notified_services": services,
            },
            reference=reference,
        )
        db.add(scenario)
        await db.flush()
        db.add(AuditLog(user_id=teacher.id, action="TICKET_IMPORTED", entity_type="scenario",
                        entity_id=scenario.id, payload={"ticket": key}))
        created += 1
    return created


async def main() -> None:
    async with session_factory.begin() as session:
        print({"created": await import_real_tickets(session, Path("/data/tickets/tickets.json"))})


if __name__ == "__main__":
    asyncio.run(main())
