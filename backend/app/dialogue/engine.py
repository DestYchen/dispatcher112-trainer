"""Boss-call dialogue graph: the duty officer asks for whatever the dispatcher's report is missing.

Runtime is cheap and deterministic: every boss line is pre-recorded, and routing a student
utterance only decides which slots it fills (address, incident type, victims, measures).
The default detector is keyword-based; a semantic router (openjev, see app.dialogue.router)
can be plugged in. Utterances that fill nothing are logged for the overnight learning job,
which proposes new examples and lines that the teacher approves before they go live.
"""

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

from app.scoring.rules.lexicon import ADDRESS, normalized

VICTIMS = re.compile(r"пострада|погиб|жертв|травм|ранен|ожог|без сознания|никто не|все целы|людей нет", re.I)
MEASURES = re.compile(r"выех|направ|на месте|прибыл|вызвал|бригад|пожарн|скор|полици|аварийн|эвакуир", re.I)
STREET_WORDS = re.compile(r"(?:ул(?:ица|\.)?|улиц[аеу]|проспект|пр-т|переулок|пер\.|проезд|шоссе|ш\.|бульвар|б-р|"
                          r"набережная|площадь|пл\.|тупик)\s*", re.I)
INCIDENT_WORDS = re.compile(
    r"пожар|задымл|горит|возгоран|дтп|авари|драк|плохо|травм|утеч|запах газа|затоп|прорыв|обрушен|"
    r"взрыв|угон|кража|хулиган|труп|тонет|пропал|суицид|скандал|шум|сигнализац|подозрит", re.I
)


class Router(Protocol):
    def slots(self, utterance: str, slots: list[str]) -> set[str]: ...


@dataclass
class Card:
    address: str
    incident_type: str


@dataclass
class Turn:
    utterance: str
    asked: str  # node id the boss played before this utterance


@dataclass
class Reply:
    node: str
    text: str
    filled: list[str]
    final: bool
    unmatched: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def load_graph(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def street_of(address: str) -> str:
    """'Москва, ул. Берзарина, дом 21' -> 'берзарина'. Best effort; empty when unknown."""
    text = normalized(address)
    for part in text.split(","):
        part = part.strip()
        if STREET_WORDS.search(part) or re.search(r"\b(ш|шоссе|проспект|проезд|бульвар)\b", part):
            name = STREET_WORDS.sub("", part).strip(" .")
            if name and not name.isdigit() and name not in ("москва",):
                return name
    return ""


def confusable_street(utterance: str, street: str) -> str | None:
    """A word that is close to the card's street but not equal: Дубнинская vs Дубининская."""
    if not street:
        return None
    target = stem(street.split()[0])
    for word in re.findall(r"[а-яё-]{5,}", normalized(utterance)):
        ratio = SequenceMatcher(None, stem(word), target).ratio()
        if stem(word) != target and 0.8 <= ratio < 1.0:
            return word
    return None


def stem(word: str) -> str:
    """Drop Russian adjective/noun case endings so 'Дубнинской' matches 'Дубнинская'."""
    return re.sub(r"(ая|ой|ую|ую|ое|ий|ый|ом|ей|ых|их|ым|им|а|у|е|ы|и|я|ю)$", "", word)


def similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, normalized(a).strip(" .!?"), normalized(b).strip(" .!?")).ratio() >= 0.85


class KeywordRouter:
    def __init__(self, card: Card, examples: dict[str, list[str]] | None = None) -> None:
        self.card = card
        self.street = street_of(card.address)
        self.examples = examples or {}

    def slots(self, utterance: str, slots: list[str]) -> set[str]:
        text = normalized(utterance)
        found = set()
        if ADDRESS.search(text) or (self.street and self.street.split()[0][:6] in text and re.search(r"\d|один|два|три|пят|шест|сем|вос|девя|десят|дцат", text)):
            found.add("address")
        head = normalized(self.card.incident_type.split(":")[0]).strip()
        if (head and head in text) or INCIDENT_WORDS.search(text):
            found.add("incident_type")
        if VICTIMS.search(text):
            found.add("victims")
        if MEASURES.search(text):
            found.add("measures")
        # Phrasings approved by the teacher after overnight learning.
        for slot, phrases in self.examples.items():
            if any(similar(text, phrase) for phrase in phrases):
                found.add(slot)
        return found & set(slots)


def step(graph: dict[str, Any], card: Card, turns: list[Turn], router: Router | None = None) -> Reply:
    """Decide the boss's next line from the whole conversation so far."""
    router = router or KeywordRouter(card, graph.get("examples"))
    slots: list[str] = graph["slots"]
    filled: set[str] = set()
    street = street_of(card.address)
    unmatched = False
    confirm: str | None = None
    for turn in turns:
        found = router.slots(turn.utterance, slots)
        wrong = confusable_street(turn.utterance, street)
        if wrong:
            # The customer's real case: a crew went to Дубининская instead of Дубнинская.
            found.discard("address")
            confirm = wrong
        elif "address" in found:
            confirm = None
        filled |= found
        unmatched = not found and not wrong
    nodes = graph["nodes"]
    if not turns:
        return Reply("greeting", nodes["greeting"]["text"], [], False)
    if confirm:
        # Pre-recorded line; the on-screen hint names the two look-alike streets.
        hint = f"{street.split()[0].capitalize()} или {confirm.capitalize()}?"
        return Reply("confirm_street", nodes["confirm_street"]["text"], sorted(filled), False,
                     extra={"heard": confirm, "expected": street, "hint": hint})
    missing = [s for s in slots if s not in filled]
    if not missing or len(turns) >= graph.get("max_turns", 6):
        return Reply("done", nodes["done"]["text"], sorted(filled), True, extra={"missing": missing})
    if turns and unmatched:
        talk = next((row for row in graph.get("smalltalk", []) if similar(turns[-1].utterance, row["utterance"])), None)
        if talk:
            return Reply("smalltalk", talk["reply"], sorted(filled), False)
        return Reply("not_understood", nodes["not_understood"]["text"], sorted(filled), False, unmatched=True)
    node = f"ask_{missing[0]}"
    return Reply(node, nodes[node]["text"], sorted(filled), False)
