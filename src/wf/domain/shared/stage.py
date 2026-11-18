"""The internal stages a ticket moves through, in workflow order."""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    BACKLOG = "backlog"
    IN_REFINEMENT = "in_refinement"
    HOTFIX_REVIEW = "hotfix_review"
    READY_FOR_DEV = "ready_for_dev"
    IN_DEV = "in_dev"
    PR_OPEN = "pr_open"
    PR_IN_REVIEW = "pr_in_review"
    READY_FOR_PERF_REVIEW = "ready_for_perf_review"
    READY_FOR_QA = "ready_for_qa"
    QA_APPROVED = "qa_approved"
    MERGED = "merged"
    REGRESSION = "regression"
    PERFORMANCE = "performance"
    DEPLOYED = "deployed"


WORKFLOW_ORDER: tuple[Stage, ...] = tuple(Stage)
