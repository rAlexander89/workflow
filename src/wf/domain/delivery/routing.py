"""Routing: which owner acts when a ticket enters a stage. Fixed rules, not a model call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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
STAGE_OWNERS: dict[str, str | None] = {
    "backlog": None,
    "in_refinement": "planning",
    "hotfix_review": "planning",
    "ready_for_dev": "dev",
    "in_dev": "dev",
    "pr_open": "pr_review",
    "pr_in_review": "pr_review",
    "ready_for_perf_review": "perf_review",
    "ready_for_qa": "qa",
    "qa_approved": "harness:merge",
    "merged": None,  # CI deploys the internal main to the dev environment
    "regression": "qa",
    "performance": "performance",
    "deployed": None,  # the performance agent picks these up later
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
    if hotfix and owner == "dev":
        owner = "oncall"
        reason = "hotfix label: the on-call agent takes development"
    elif hotfix and owner == "planning":
        reason = "hotfix review with Product and Staff"
    else:
        reason = f"{status!r} is owned by {owner!r}"

    if owner == "harness:merge":
        return Decision("merge", owner, reason)
    return Decision("dispatch", owner, reason)
