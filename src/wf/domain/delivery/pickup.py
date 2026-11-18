"""Dev pickup: which ready tickets the dev agent may take."""

from __future__ import annotations

PICKUP_FROM = "ready_for_dev"
PICKUP_TO = "in_dev"


def skip_reason(sprint_state: str | None, flags: list[str], jira_target: str | None) -> str | None:
    """Why a ready ticket is not picked up, or None when it can be."""
    if sprint_state != "active":
        return "not in an active sprint"
    if flags:
        return f"flagged: {', '.join(flags)}"
    if jira_target is None:
        return f"no Jira status is mapped to {PICKUP_TO}"
    return None
