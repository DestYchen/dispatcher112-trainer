from app.scoring.types import ScoreInput, manual_events


def missing_comment(data: ScoreInput) -> bool:
    events = manual_events(data)
    if data.reference.get("comment_required") and not any(
        len((event.get("comment") or "").strip()) >= 15 for event in events
    ):
        return True
    return any(
        event["status"] in {"NOT_ACCEPTED", "WORK_REFUSED"}
        and len((event.get("comment") or "").strip()) < 15
        for event in events
    )
