"""Routing: which owner acts when a ticket enters a stage. Fixed rules, not a model call."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from wf.domain.shared.stage import Stage


class Owner(StrEnum):
    """Who acts in a stage. A role, or one of two arrangements that are not a single role."""
    PLANNING = "planning"  # Product and Staff, with the user (refinement)
    DEV = "dev"
    ONCALL = "oncall"
    PR_REVIEW = "pr_review"
    PERF_REVIEW = "perf_review"
    QA = "qa"
    PERFORMANCE = "performance"
    HARNESS_MERGE = "harness:merge"  # the harness merges once both approvals exist


HOTFIX_LABEL = "hotfix"
MAX_DISPATCHES_PER_STATUS = 10  # loop guard; a ticket bouncing this often needs a human

# Owner per internal stage. None = no agent acts in this stage alone.
#   "product"       the Product agent
#   "planning"      Product and Staff, with the user (refinement)
#   "dev"           the Dev agent
#   "pr_review"     the PR review agent
#   "perf_review"   the performance reviewer of the code
#   "performance"   the performance agent, when the dev asked for a check
#   "qa"            the QA agent
#   "harness:merge" the harness merges to the internal main once both approvals exist
STAGE_OWNERS: dict[Stage, Owner | None] = {
    Stage.BACKLOG: None,
    Stage.IN_REFINEMENT: Owner.PLANNING,
    Stage.HOTFIX_REVIEW: Owner.PLANNING,
    Stage.READY_FOR_DEV: Owner.DEV,
    Stage.IN_DEV: Owner.DEV,
    Stage.PR_OPEN: Owner.PR_REVIEW,
    Stage.PR_IN_REVIEW: Owner.PR_REVIEW,
    Stage.READY_FOR_PERF_REVIEW: Owner.PERF_REVIEW,
    Stage.READY_FOR_QA: Owner.QA,
    Stage.QA_APPROVED: Owner.HARNESS_MERGE,
    Stage.MERGED: None,  # CI deploys the internal main to the dev environment
    Stage.REGRESSION: Owner.QA,
    Stage.PERFORMANCE: Owner.PERFORMANCE,
    Stage.DEPLOYED: None,  # the performance agent picks these up later
}


@dataclass(frozen=True)
class Decision:
    action: str  # dispatch | merge | none | unknown | escalate | halt
    owner: str | None
    reason: str


def route(
    status: str,
    labels: set[str] | frozenset[str] = frozenset(),
    status_to_stage: Mapping[str, str] | None = None,
) -> Decision:
    """Decide what happens when a ticket enters `status`.

    `status_to_stage` comes from the board's stage mapping. Without it, or for a status
    that isn't mapped, nothing routes.
    """
    if not status_to_stage or status not in status_to_stage:
        return Decision("unknown", None, f"status {status!r} is not mapped to a stage")
    stage = status_to_stage[status]
    owner = STAGE_OWNERS[stage]
    if owner is None:
        return Decision("none", None, f"{status!r} ({stage}) has no owning agent")

    hotfix = HOTFIX_LABEL in labels
    if hotfix and owner == Owner.DEV:
        owner = Owner.ONCALL
        reason = "hotfix label: the on-call agent takes development"
    elif hotfix and owner == Owner.PLANNING:
        reason = "hotfix review with Product and Staff"
    else:
        reason = f"{status!r} is owned by {owner!r}"

    if owner == Owner.HARNESS_MERGE:
        return Decision("merge", owner, reason)
    return Decision("dispatch", owner, reason)
