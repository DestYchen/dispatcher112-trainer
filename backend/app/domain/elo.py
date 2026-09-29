"""Elo ratings for students and scenarios, replayed from scored attempts.

Every closed attempt is a "game" between a student and a scenario, like a chess puzzle on
Lichess. The outcome is the effective score (teacher override wins) scaled to 0..1. Before each
update we record the predicted outcome, so the same replay yields an honest forecast check:
prediction is made only from attempts that happened earlier.

Ratings are recomputed from history on demand: no stored state to drift, and a teacher's
correction of an old score is reflected on the next read.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

BASE = 1000.0
SCALE = 400.0
K_STUDENT = 32.0
K_SCENARIO = 16.0  # scenarios are played by many students, so they move slower
DIFFICULTY_STEP = 100.0  # prior: difficulty 5 = 1000, each level = +/-100


@dataclass(frozen=True)
class Attempt:
    student_id: UUID
    scenario_id: UUID
    difficulty: int
    total: float  # 0..100
    at: datetime


@dataclass
class Replay:
    students: dict[UUID, float] = field(default_factory=dict)
    scenarios: dict[UUID, float] = field(default_factory=dict)
    games: dict[UUID, int] = field(default_factory=dict)
    forecasts: list[tuple[float, float]] = field(default_factory=list)  # (predicted, actual)


def prior(difficulty: int) -> float:
    return BASE + (difficulty - 5) * DIFFICULTY_STEP


def expected(student: float, scenario: float) -> float:
    return 1.0 / (1.0 + 10 ** ((scenario - student) / SCALE))


def replay(attempts: list[Attempt]) -> Replay:
    state = Replay()
    for a in sorted(attempts, key=lambda row: row.at):
        s = state.students.get(a.student_id, BASE)
        t = state.scenarios.get(a.scenario_id, prior(a.difficulty))
        p = expected(s, t)
        actual = max(0.0, min(1.0, a.total / 100.0))
        state.forecasts.append((p, actual))
        state.students[a.student_id] = s + K_STUDENT * (actual - p)
        state.scenarios[a.scenario_id] = t - K_SCENARIO * (actual - p)
        state.games[a.student_id] = state.games.get(a.student_id, 0) + 1
    return state


def calibration(forecasts: list[tuple[float, float]], bins: int = 5) -> dict[str, object]:
    """Mean absolute error, and predicted-vs-actual per probability bin (for a reliability chart)."""
    if not forecasts:
        return {"attempts": 0, "mae": None, "baseline_mae": None, "bins": []}
    mae = sum(abs(p - a) for p, a in forecasts) / len(forecasts)
    mean_actual = sum(a for _, a in forecasts) / len(forecasts)
    baseline = sum(abs(mean_actual - a) for _, a in forecasts) / len(forecasts)
    rows = []
    for i in range(bins):
        low, high = i / bins, (i + 1) / bins
        chunk = [(p, a) for p, a in forecasts if low <= p < high or (i == bins - 1 and p == 1.0)]
        if chunk:
            rows.append({
                "from": low, "to": high, "count": len(chunk),
                "predicted": round(sum(p for p, _ in chunk) / len(chunk), 3),
                "actual": round(sum(a for _, a in chunk) / len(chunk), 3),
            })
    return {"attempts": len(forecasts), "mae": round(mae, 3), "baseline_mae": round(baseline, 3), "bins": rows}


def recommend(student: float, candidates: dict[UUID, float], target: float = 0.7,
              exclude: set[UUID] | None = None, limit: int = 5) -> list[tuple[UUID, float]]:
    """Scenarios whose predicted success is closest to the target ("stretch but doable")."""
    pool = [(sid, expected(student, rating)) for sid, rating in candidates.items() if sid not in (exclude or set())]
    pool.sort(key=lambda row: abs(row[1] - target))
    return pool[:limit]


def level(rating: float) -> int:
    """Map a rating to the customer's 1..10 difficulty scale."""
    return max(1, min(10, round(5 + (rating - BASE) / DIFFICULTY_STEP)))
