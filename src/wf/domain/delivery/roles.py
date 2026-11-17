"""Which role runs in each stage, the limits that end a loop, and the merge approvals."""

from __future__ import annotations

# Which role runs in each stage. Stages not listed have no agent.
STAGE_ROLE: dict[str, str | None] = {
    "in_refinement": "product",  # product and staff take turns here: see next_refinement_role
    "hotfix_review": "staff",
    "ready_for_dev": "dev",
    "in_dev": "dev",
    "pr_open": "pr_review",
    "pr_in_review": "pr_review",
    "ready_for_perf_review": "perf_review",
    "ready_for_qa": "qa",
    "merged": None,  # the harness moves it on to regression
    "regression": "qa",
    "performance": "performance",
}

REFINEMENT_STAGES = ("in_refinement",)
MAX_RUNS_PER_STAGE = 3  # loop guard: a ticket that fails this often in a row needs a human
MAX_REFINEMENT_ROUNDS = 7  # product rounds without the staff bot's agreement: a person is asked
DONE_FOR_BLOCKING = ("merged", "regression", "performance", "deployed")  # a prerequisite is on dev
PAUSABLE = ("in_dev", "pr_open", "pr_in_review")  # dev-owned work a kicked-back prerequisite interrupts

# A ticket merges only when each of these roles approved its current commit.
REQUIRED_APPROVALS = frozenset({"pr_review", "perf_review", "qa"})


def next_refinement_role(last_role: str | None) -> str:
    """Whose turn it is in refinement: product goes first, then staff, and so on."""
    return "product" if last_role is None or last_role == "staff" else "staff"


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
