from app.domain.enums import ResponseStatus as S


class InvalidTransition(ValueError):
    code = "INVALID_TRANSITION"


class CommentRequired(ValueError):
    code = "COMMENT_REQUIRED"


TRANSITIONS: dict[S | None, tuple[S, ...]] = {
    None: (S.ACCEPTED, S.NOT_ACCEPTED),
    S.ADDED: (S.RECEIVED,),
    S.RECEIVED: (S.ACCEPTED, S.NOT_ACCEPTED),
    S.NOT_ACCEPTED: (S.ACCEPTED,),
    S.ACCEPTED: (
        S.RESPONSE_STARTED,
        S.ARRIVED,
        S.WORK_IN_PROGRESS,
        S.WORK_COMPLETED,
        S.WORK_REFUSED,
    ),
    S.RESPONSE_STARTED: (S.ARRIVED, S.WORK_IN_PROGRESS, S.WORK_COMPLETED, S.WORK_REFUSED),
    S.ARRIVED: (S.WORK_IN_PROGRESS, S.WORK_COMPLETED, S.WORK_REFUSED),
    S.WORK_IN_PROGRESS: (S.WORK_COMPLETED, S.WORK_REFUSED),
    S.WORK_COMPLETED: (),
    S.WORK_REFUSED: (),
}


def available_transitions(current: S | None) -> list[S]:
    return list(TRANSITIONS[current])


def validate_transition(current: S | None, target: S, comment: str | None) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition("Этот переход статуса недоступен.")
    if target in {S.NOT_ACCEPTED, S.WORK_REFUSED} and len((comment or "").strip()) < 15:
        raise CommentRequired(
            "Укажите причину отказа и куда передана информация: не менее 15 символов."
        )


def is_terminal(status: S) -> bool:
    return status in {S.WORK_COMPLETED, S.WORK_REFUSED}
