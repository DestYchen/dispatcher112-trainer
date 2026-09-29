"""Teacher view of adaptive difficulty: Elo ratings, forecast check, next-task suggestions."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, Teacher
from app.api.errors import APIError
from app.db.models import Assignment, Scenario, User
from app.domain import elo
from app.scoring.effective import effective_score

router = APIRouter(tags=["elo"])


async def load(db: DB) -> tuple[elo.Replay, dict[UUID, Scenario], dict[UUID, User], dict[UUID, set[UUID]]]:
    rows = (
        await db.execute(
            select(Assignment, Scenario)
            .join(Scenario, Scenario.id == Assignment.scenario_id)
            .where(Assignment.score.is_not(None), Assignment.closed_at.is_not(None))
        )
    ).all()
    attempts = [
        elo.Attempt(
            student_id=a.student_id,
            scenario_id=s.id,
            difficulty=s.difficulty,
            total=float(effective_score(a.score, a.teacher_override)["total"]),
            at=a.closed_at,
        )
        for a, s in rows
        if a.score and a.closed_at
    ]
    played: dict[UUID, set[UUID]] = {}
    for a in attempts:
        played.setdefault(a.student_id, set()).add(a.scenario_id)
    scenarios = {s.id: s for s in await db.scalars(select(Scenario).where(Scenario.status == "APPROVED"))}
    students = {u.id: u for u in await db.scalars(select(User).where(User.role == "STUDENT"))}
    return elo.replay(attempts), scenarios, students, played


def name(user: User) -> str:
    return " ".join(part for part in (user.last_name, user.first_name) if part) or user.login


@router.get("/teacher/elo")
async def overview(db: DB, user: Teacher) -> dict[str, Any]:
    state, scenarios, students, _ = await load(db)
    return {
        "students": sorted(
            (
                {
                    "id": str(sid),
                    "name": name(u),
                    "rating": round(state.students.get(sid, elo.BASE)),
                    "level": elo.level(state.students.get(sid, elo.BASE)),
                    "attempts": state.games.get(sid, 0),
                }
                for sid, u in students.items()
            ),
            key=lambda row: -row["rating"],
        ),
        "scenarios": sorted(
            (
                {
                    "id": str(sid),
                    "title": s.title,
                    "difficulty_prior": s.difficulty,
                    "rating": round(state.scenarios.get(sid, elo.prior(s.difficulty))),
                    "level": elo.level(state.scenarios.get(sid, elo.prior(s.difficulty))),
                    "played": sid in state.scenarios,
                }
                for sid, s in scenarios.items()
            ),
            key=lambda row: -row["rating"],
        ),
        "forecast": elo.calibration(state.forecasts),
    }


@router.get("/teacher/elo/recommend")
async def recommend(
    db: DB,
    user: Teacher,
    student_id: UUID,
    target: float = Query(0.7, ge=0.05, le=0.95),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    state, scenarios, students, played = await load(db)
    if student_id not in students:
        raise APIError(404, "not_found", "Обучающийся не найден.")
    rating = state.students.get(student_id, elo.BASE)
    candidates = {sid: state.scenarios.get(sid, elo.prior(s.difficulty)) for sid, s in scenarios.items()}
    picks = elo.recommend(rating, candidates, target, played.get(student_id), limit)
    return {
        "student_rating": round(rating),
        "target": target,
        "items": [
            {
                "scenario_id": str(sid),
                "title": scenarios[sid].title,
                "rating": round(candidates[sid]),
                "predicted_success": round(p, 3),
            }
            for sid, p in picks
        ],
    }
