from app.scoring.types import ScoreInput, manual_events


def status_mismatch(data: ScoreInput) -> bool:
    actual = [event["status"] for event in manual_events(data)]
    expected = data.reference.get("expected_status_chain", [data.reference["expected_status"]])
    if not actual:
        return True
    if data.reference["expected_status"] == "NOT_ACCEPTED":
        return any(status not in {"NOT_ACCEPTED"} for status in actual)
    # The reference chain expresses the factual outcome; completing unfinished work is a mismatch.
    if expected and expected[-1] in {"WORK_COMPLETED", "WORK_REFUSED"}:
        return bool(actual[-1] != expected[-1])
    return bool(actual[0] != data.reference["expected_status"])
