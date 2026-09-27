from app.scoring.types import ScoreInput, manual_events


def refused_own(data: ScoreInput) -> bool:
    return data.reference["expected_status"] == "ACCEPTED" and any(
        event["status"] in {"NOT_ACCEPTED", "WORK_REFUSED"} for event in manual_events(data)
    )
