"""Who does things on a ticket: the agent roles, and the other actors that leave a mark on its history."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """An agent role. Each role has a prompt, a set of tools, and the stages it owns."""
    PRODUCT = "product"
    STAFF = "staff"
    DEV = "dev"
    ONCALL = "oncall"
    PR_REVIEW = "pr_review"
    PERF_REVIEW = "perf_review"
    QA = "qa"
    PERFORMANCE = "performance"


class Actor(StrEnum):
    """A non-agent actor. A role is also an actor, so history records `actor` as plain text."""
    USER = "user"
    HARNESS = "harness"
    SYNC = "sync"
    OUTBOX = "outbox"
    JIRA_AGENT = "jira-agent"
    OBSERVER = "observer"
    MERGE_BOT = "merge_bot"


# Roles that may record an approval of a commit.
APPROVER_ROLES: tuple[Role, ...] = (Role.PR_REVIEW, Role.PERF_REVIEW, Role.QA, Role.PERFORMANCE)
