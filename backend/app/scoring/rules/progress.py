from app.scoring.types import ScoreInput, manual_events


def skipped_progress(data: ScoreInput) -> bool:
    expected = [
        status
        for status in data.reference.get("expected_status_chain", [])
        if status in {"RESPONSE_STARTED", "ARRIVED", "WORK_IN_PROGRESS"}
    ]
    actual = [event["status"] for event in manual_events(data)]
    return any(status not in actual for status in expected)
