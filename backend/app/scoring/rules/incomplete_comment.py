from app.scoring.rules.lexicon import satisfies
from app.scoring.types import ScoreInput, manual_events


def missing_entities(data: ScoreInput) -> list[str]:
    text = " ".join(str(event.get("comment") or "") for event in manual_events(data))
    return [
        requirement
        for requirement in data.reference.get("comment_must_contain", [])
        if not satisfies(requirement, text, data.services)
    ]
