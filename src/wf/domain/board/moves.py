"""Who may move a ticket from one internal stage to another."""

from __future__ import annotations

from wf.domain.shared.roles import Actor, Role
from wf.domain.shared.stage import Stage

from enum import StrEnum


class Flag(StrEnum):
    NEEDS_HUMAN = "needs-human"
    BLOCKED = "blocked"


FLAGS = tuple(Flag)

# Who may move a ticket from one internal stage to another. Derived from the agent
# prompts. The user may move anything. This list is not complete: add a row when a
# prompt gains a new allowed move. Anything not listed is refused.
ALLOWED_MOVES: frozenset[tuple[str, str | None, str]] = frozenset({
    (Role.STAFF, Stage.IN_REFINEMENT, Stage.READY_FOR_DEV),
    (Actor.HARNESS, Stage.BACKLOG, Stage.IN_REFINEMENT),
    (Actor.HARNESS, Stage.IN_DEV, Stage.READY_FOR_DEV),
    (Actor.HARNESS, Stage.PR_OPEN, Stage.READY_FOR_DEV),
    (Actor.HARNESS, Stage.PR_IN_REVIEW, Stage.READY_FOR_DEV),
    (Role.DEV, Stage.READY_FOR_DEV, Stage.IN_DEV),
    (Role.DEV, Stage.READY_FOR_DEV, Stage.PR_OPEN),
    (Role.DEV, Stage.IN_DEV, Stage.PR_OPEN),
    (Role.DEV, Stage.PR_IN_REVIEW, Stage.IN_DEV),
    (Role.DEV, Stage.QA_APPROVED, Stage.MERGED),
    (Role.PR_REVIEW, Stage.PR_OPEN, Stage.IN_DEV),
    (Role.PR_REVIEW, Stage.PR_IN_REVIEW, Stage.IN_DEV),
    (Role.QA, Stage.READY_FOR_QA, Stage.QA_APPROVED),
    (Role.QA, Stage.READY_FOR_QA, Stage.IN_DEV),
    (Role.QA, Stage.REGRESSION, Stage.IN_DEV),
    (Actor.HARNESS, Stage.PR_IN_REVIEW, Stage.READY_FOR_QA),
    (Role.PR_REVIEW, Stage.PR_OPEN, Stage.READY_FOR_PERF_REVIEW),
    (Role.PR_REVIEW, Stage.PR_IN_REVIEW, Stage.READY_FOR_PERF_REVIEW),
    (Actor.HARNESS, Stage.PR_OPEN, Stage.READY_FOR_PERF_REVIEW),
    (Actor.HARNESS, Stage.PR_IN_REVIEW, Stage.READY_FOR_PERF_REVIEW),
    (Actor.HARNESS, Stage.READY_FOR_PERF_REVIEW, Stage.READY_FOR_QA),
    (Role.PERF_REVIEW, Stage.READY_FOR_PERF_REVIEW, Stage.READY_FOR_QA),
    (Role.PERF_REVIEW, Stage.READY_FOR_PERF_REVIEW, Stage.IN_DEV),
    (Role.QA, Stage.REGRESSION, Stage.DEPLOYED),
    (Actor.HARNESS, Stage.MERGED, Stage.REGRESSION),
    (Actor.HARNESS, Stage.REGRESSION, Stage.DEPLOYED),
    (Actor.HARNESS, Stage.PERFORMANCE, Stage.DEPLOYED),
    (Role.QA, Stage.REGRESSION, Stage.PERFORMANCE),
    (Actor.HARNESS, Stage.REGRESSION, Stage.PERFORMANCE),
    (Actor.HARNESS, Stage.QA_APPROVED, Stage.MERGED),
})


def may_move(actor: str, from_stage: str | None, to_stage: str) -> bool:
    if actor == Actor.USER:
        return True
    return (actor, from_stage, to_stage) in ALLOWED_MOVES
