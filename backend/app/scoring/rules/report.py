import re

from app.scoring.rules.lexicon import ADDRESS, normalized
from app.scoring.types import ScoreInput


def report_complete(data: ScoreInput) -> bool:
    if not data.reference.get("report_required"):
        return True
    for report in data.reports:
        if report.get("callee_code") != data.reference.get("report_callee_code"):
            continue
        text = normalized(report.get("transcript") or "")
        if not text.strip():
            continue
        facts = {
            "address": bool(ADDRESS.search(text)),
            "incident_type": bool(
                data.incident_type_name
                and normalized(data.incident_type_name.split(":")[0]) in text
            ),
            "victims": bool(re.search(r"пострада|погиб|жертв|травм|ранен", text)),
        }
        if all(facts.get(key, False) for key in data.reference.get("report_must_mention", [])):
            return True
    return False
