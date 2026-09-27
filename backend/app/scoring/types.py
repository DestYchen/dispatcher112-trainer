from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ScoreInput:
    delivered_at: datetime
    opened_at: datetime | None
    primary_status_at: datetime | None
    closed_at: datetime
    settings: dict[str, Any]
    reference: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    reports: tuple[dict[str, Any], ...] = ()
    services: tuple[str, ...] = ()
    card_number: str = ""
    address: str = ""
    incident_type_name: str = ""
    grammar_items: tuple[dict[str, Any], ...] = ()
    grammar_available: bool = True
    grammar_skip_reason: str | None = None
    address_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    entry_fields: tuple[dict[str, Any], ...] | None = None
    card_submitted: bool = False


def manual_events(data: ScoreInput) -> list[dict[str, Any]]:
    return [event for event in data.events if not event.get("is_automatic", False)]
