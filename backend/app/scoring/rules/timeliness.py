def deadline_score(delay_ms: int | None, deadline_ms: int) -> float:
    if delay_ms is None or delay_ms > deadline_ms * 2:
        return 0.0
    if delay_ms <= deadline_ms:
        return 100.0
    return 100.0 - 60.0 * (delay_ms - deadline_ms) / deadline_ms
