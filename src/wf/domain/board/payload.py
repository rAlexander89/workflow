"""The shape of a sync payload from the Jira session, and the result of applying one."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wf.domain.board.errors import BoardError
from wf.domain.shared.sprint import SprintState

SPRINT_STATES = tuple(SprintState)
STATUS_CATEGORIES = ("new", "indeterminate", "done")
SYNC_KINDS = ("active", "past", "backlog")


def _need(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise BoardError(f"{where}: missing {key!r}")
    return obj[key]


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise BoardError(f"{key!r} must be a list")
    return value


def validate(data: Any) -> dict:
    """Check the payload's shape. Returns it unchanged on success."""
    if not isinstance(data, dict):
        raise BoardError("payload must be a JSON object")
    kind = _need(data, "kind", "payload")
    if kind not in SYNC_KINDS:
        raise BoardError(f"kind must be one of {SYNC_KINDS}, got {kind!r}")
    for p in _list(data, "projects"):
        _need(p, "key", "project")
        _need(p, "name", f"project {p.get('key')}")
        for s in _list(p, "statuses"):
            name = _need(s, "name", f"status in {p['key']}")
            if _need(s, "category", f"status {name!r}") not in STATUS_CATEGORIES:
                raise BoardError(f"status {name!r}: category must be one of {STATUS_CATEGORIES}")
    for s in _list(data, "sprints"):
        sid = _need(s, "id", "sprint")
        if _need(s, "state", f"sprint {sid}") not in SPRINT_STATES:
            raise BoardError(f"sprint {sid}: state must be one of {SPRINT_STATES}")
        _need(s, "name", f"sprint {sid}")
        _need(s, "board_id", f"sprint {sid}")
    for t in _list(data, "tickets"):
        key = _need(t, "key", "ticket")
        for field_name in ("project_key", "jira_id", "summary", "status"):
            _need(t, field_name, f"ticket {key}")
        if not isinstance(t.get("labels", []), list):
            raise BoardError(f"ticket {key}: labels must be a list")
    for c in _list(data, "comments"):
        _need(c, "ticket_key", "comment")
        _need(c, "jira_comment_id", "comment")
        _need(c, "author", "comment")
        _need(c, "body", "comment")
        _need(c, "created", "comment")
    return data


@dataclass
class SyncResult:
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def bump(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n
