from typing import Any


def effective_score(score: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Apply the teacher's separate correction without changing engine output."""
    correction = override or {}
    axes = {
        name: {**axis, "score": correction.get("axes", {}).get(name, axis["score"])}
        for name, axis in score["axes"].items()
    }
    total = correction.get("total")
    if total is None:
        total = round(sum((axis["score"] or 0) * axis["weight"] for axis in axes.values()), 2)
    return {"total": total, "axes": axes}
