import ast
import inspect
from itertools import product

import pytest

from app.domain import status_machine
from app.domain.enums import ResponseStatus
from app.domain.status_machine import (
    CommentRequired,
    InvalidTransition,
    available_transitions,
    is_terminal,
    validate_transition,
)

# Independent transcription of SPEC 4.2, including PLAN's manual-history None.
EXPECTED = {
    None: ["ACCEPTED", "NOT_ACCEPTED"],
    "ADDED": ["RECEIVED"],
    "RECEIVED": ["ACCEPTED", "NOT_ACCEPTED"],
    "NOT_ACCEPTED": ["ACCEPTED"],
    "ACCEPTED": [
        "RESPONSE_STARTED",
        "ARRIVED",
        "WORK_IN_PROGRESS",
        "WORK_COMPLETED",
        "WORK_REFUSED",
    ],
    "RESPONSE_STARTED": ["ARRIVED", "WORK_IN_PROGRESS", "WORK_COMPLETED", "WORK_REFUSED"],
    "ARRIVED": ["WORK_IN_PROGRESS", "WORK_COMPLETED", "WORK_REFUSED"],
    "WORK_IN_PROGRESS": ["WORK_COMPLETED", "WORK_REFUSED"],
    "WORK_COMPLETED": [],
    "WORK_REFUSED": [],
}
ALL = [value for value in EXPECTED if value is not None]


@pytest.mark.parametrize("current,target", list(product(EXPECTED, ALL)))
def test_every_pair(current: str | None, target: str) -> None:
    source = ResponseStatus(current) if current else None
    destination = ResponseStatus(target)
    if target in EXPECTED[current]:
        validate_transition(source, destination, "Причина отказа указана полностью.")
    else:
        with pytest.raises(InvalidTransition):
            validate_transition(source, destination, "Причина отказа указана полностью.")


@pytest.mark.parametrize("current", list(EXPECTED))
def test_available_and_terminal(current: str | None) -> None:
    source = ResponseStatus(current) if current else None
    assert [status.value for status in available_transitions(source)] == EXPECTED[current]
    if source:
        assert is_terminal(source) is (current in {"WORK_COMPLETED", "WORK_REFUSED"})


@pytest.mark.parametrize(
    "current,target", [("RECEIVED", "NOT_ACCEPTED"), ("ACCEPTED", "WORK_REFUSED")]
)
@pytest.mark.parametrize("comment", [None, "", "   ", "а" * 14, " " * 20 + "а" * 14])
def test_comments_required(current: str, target: str, comment: str | None) -> None:
    with pytest.raises(CommentRequired):
        validate_transition(ResponseStatus(current), ResponseStatus(target), comment)


def test_fifteen_characters_accepted_and_no_shared_mutable_state() -> None:
    validate_transition(ResponseStatus.RECEIVED, ResponseStatus.NOT_ACCEPTED, "а" * 15)
    available_transitions(None).clear()
    assert len(available_transitions(None)) == 2


def test_domain_module_does_not_import_database_or_models() -> None:
    tree = ast.parse(inspect.getsource(status_machine))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "app.domain.enums"
        assert not isinstance(node, ast.Import)
