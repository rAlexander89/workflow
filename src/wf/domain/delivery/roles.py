"""Which role runs in each stage, the limits that end a loop, and the merge approvals."""

from __future__ import annotations

from wf.domain.shared.roles import Role
from wf.domain.shared.stage import Stage

# Which role runs in each stage. Stages not listed have no agent.
STAGE_ROLE: dict[Stage, Role | None] = {
    Stage.IN_REFINEMENT: Role.PRODUCT,  # product and staff take turns here: see next_refinement_role
    Stage.HOTFIX_REVIEW: Role.STAFF,
    Stage.READY_FOR_DEV: Role.DEV,
    Stage.IN_DEV: Role.DEV,
    Stage.PR_OPEN: Role.PR_REVIEW,
    Stage.PR_IN_REVIEW: Role.PR_REVIEW,
    Stage.READY_FOR_PERF_REVIEW: Role.PERF_REVIEW,
    Stage.READY_FOR_QA: Role.QA,
    Stage.MERGED: None,  # the harness moves it on to regression
    Stage.REGRESSION: Role.QA,
    Stage.PERFORMANCE: Role.PERFORMANCE,
}

REFINEMENT_STAGES = (Stage.IN_REFINEMENT,)
MAX_RUNS_PER_STAGE = 3  # loop guard: a ticket that fails this often in a row needs a human
MAX_REFINEMENT_ROUNDS = 7  # product rounds without the staff bot's agreement: a person is asked
DONE_FOR_BLOCKING = (Stage.MERGED, Stage.REGRESSION, Stage.PERFORMANCE, Stage.DEPLOYED)  # a prerequisite is on dev
PAUSABLE = (Stage.IN_DEV, Stage.PR_OPEN, Stage.PR_IN_REVIEW)  # dev-owned work a kicked-back prerequisite interrupts

# A ticket merges only when each of these roles approved its current commit.
REQUIRED_APPROVALS = frozenset({Role.PR_REVIEW, Role.PERF_REVIEW, Role.QA})


def next_refinement_role(last_role: str | None) -> str:
    """Whose turn it is in refinement: product goes first, then staff, and so on."""
    return Role.PRODUCT if last_role is None or last_role == Role.STAFF else Role.STAFF


def refinement_limit_reached(product_rounds: int) -> bool:
    return product_rounds >= MAX_REFINEMENT_ROUNDS


def run_limit_reached(failed_in_a_row: int) -> bool:
    return failed_in_a_row >= MAX_RUNS_PER_STAGE


def missing_approvals(approved: set[str] | frozenset[str]) -> frozenset[str]:
    """The required approvals not yet given at the current commit."""
    return REQUIRED_APPROVALS - set(approved)


def unmet_blockers(blockers: list[str], stage_of: dict[str, str]) -> list[str]:
    """Prerequisites of a ticket that are not on dev yet."""
    return [b for b in blockers if stage_of.get(b) not in DONE_FOR_BLOCKING]
