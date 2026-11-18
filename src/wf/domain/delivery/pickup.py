"""Dev pickup: which ready tickets the dev agent may take."""

from __future__ import annotations

from wf.domain.shared.sprint import SprintState
from wf.domain.shared.stage import Stage

PICKUP_FROM = Stage.READY_FOR_DEV
PICKUP_TO = Stage.IN_DEV


def skip_reason(sprint_state: str | None, flags: list[str], jira_target: str | None) -> str | None:
    """Why a ready ticket is not picked up, or None when it can be."""
    if sprint_state != SprintState.ACTIVE:
        return "not in an active sprint"
    if flags:
        return f"flagged: {', '.join(flags)}"
    if jira_target is None:
        return f"no Jira status is mapped to {PICKUP_TO}"
    return None
