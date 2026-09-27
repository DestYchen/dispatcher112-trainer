from app.scoring.types import ScoreInput, manual_events


def wrong_primary(data: ScoreInput) -> bool:
    events = manual_events(data)
    return not events or events[0]["status"] != data.reference["expected_status"]
