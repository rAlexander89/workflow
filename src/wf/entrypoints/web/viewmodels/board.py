"""View models for the board pages: the active sprint, the backlog, one item, the outbox, and the deploy panel."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from wf.application.board.status import board_status
from wf.application.tracker.outbox import RETRYABLE
from wf.domain.board import stages
from wf.domain.board.errors import BoardError
from wf.entrypoints.web import pipeline as stage_view
from wf.entrypoints.web.viewmodels.base import Screen, ViewModel

UNMAPPED = "(unmapped)"


def group_by_stage(rows) -> list[tuple[str, list[dict]]]:
    """Rows grouped in workflow order. Tickets with no stage come last."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["stage"] or UNMAPPED, []).append(dict(r))
    ordered = [(s, groups[s]) for s in stages.STAGES if s in groups]
    if UNMAPPED in groups:
        ordered.append((UNMAPPED, groups[UNMAPPED]))
    return ordered


def when(ts, fmt: str = "%b %d, %Y %H:%M") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt) if ts else ""


def sync_line(session, kind: str | None = None) -> dict:
    """How fresh the board is, and what is waiting for Jira, as the pages show it."""
    status = board_status(session.queries, time.time(), kind)
    run, age = status["last_sync"], status["age_seconds"]
    return {
        "status": status,
        "run": dict(run) if run else None,
        "age_text": None if age is None else f"{age // 3600}h {age % 3600 // 60}m ago",
        "stale": age is not None and age > 24 * 3600,
        "pending": len(session.outbox.listing("pending")),
    }


@dataclass
class BoardViewModel(ViewModel):
    """The active sprint, grouped by stage."""
    template = "board.html"
    active = "board"
    groups: list = field(default_factory=list)
    total: int = 0
    blocked: list = field(default_factory=list)
    sync: dict = field(default_factory=dict)
    title: str = "Active sprint"

    @classmethod
    def load(cls, container) -> "BoardViewModel":
        with container.session() as s:
            rows = s.queries.active_board()
            return cls(groups=group_by_stage(rows), total=len(rows),
                       blocked=[dict(r) for r in s.queries.unmapped_stages()], sync=sync_line(s, "active"))


@dataclass
class BacklogViewModel(ViewModel):
    """The user's tickets that are in no sprint, newest first."""
    template = "backlog.html"
    active = "backlog"
    tickets: list = field(default_factory=list)
    sent: bool = False
    active_sprint: dict | None = None
    sync: dict = field(default_factory=dict)

    @classmethod
    def load(cls, container, sent: bool = False) -> "BacklogViewModel":
        with container.session() as s:
            rows = []
            for r in s.queries.backlog():
                t = dict(r)
                parts = (t.get("assignee_name") or "").split()
                t["initials"] = "".join(p[0] for p in parts[:2]).upper()
                t["stage_label"] = stage_view.STAGE_INFO.get(t["stage"], ("To do", ""))[0] if t["stage"] else "To do"
                t["updated_text"] = when(t.get("jira_updated"), "%b %d")
                rows.append(t)
            rows.sort(key=lambda t: t.get("jira_updated") or 0, reverse=True)
            return cls(tickets=rows, sent=bool(sent), active_sprint=s.queries.active_sprint(), sync=sync_line(s))


@dataclass
class ItemViewModel(ViewModel):
    """One ticket from the backlog's point of view: its details, links, comments, and history."""
    template = "item.html"
    active = "backlog"
    ticket: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    events: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    stage: str | None = None
    stage_label: str = ""
    active_sprint: dict | None = None
    in_active_sprint: bool = False
    sprint_name: str | None = None
    updated_text: str = ""
    synced_text: str = ""

    @classmethod
    def load(cls, container, key: str) -> "ItemViewModel | None":
        with container.session() as s:
            detail = s.queries.ticket_detail(key)
            if detail is None:
                return None
            t = detail["ticket"]
            active = s.queries.active_sprint()
            stage = t["stage"]
            return cls(
                ticket=t, links=s.queries.ticket_links(key),
                comments=[dict(x, when=when(x["created_at"])) for x in detail["comments"]],
                events=[dict(x, when=when(x["at"])) for x in reversed(detail["events"])],
                labels=json.loads(t["labels"] or "[]"), stage=stage,
                stage_label=stage_view.STAGE_INFO.get(stage, ("To do", ""))[0] if stage else "Not started",
                active_sprint=dict(active) if active else None,
                in_active_sprint=bool(active and t["sprint_id"] == active["id"]),
                sprint_name=s.queries.sprint_name(t["sprint_id"]) if t["sprint_id"] else None,
                updated_text=when(t.get("jira_updated")), synced_text=when(t.get("synced_at")))


@dataclass
class OutboxViewModel(ViewModel):
    """The moves waiting to go to Jira, and what happened to each."""
    template = "outbox.html"
    active = "outbox"
    items: list = field(default_factory=list)
    retryable: tuple = RETRYABLE
    title: str = "Outbox"

    @classmethod
    def load(cls, container) -> "OutboxViewModel":
        with container.session() as s:
            return cls(items=s.outbox.listing())

    @staticmethod
    def retry(container, item_id: int) -> None:
        """Put an item back to pending. An item that cannot be retried is left alone."""
        try:
            with container.session() as s:
                s.outbox.retry(item_id)
        except BoardError:
            pass


@dataclass
class DeployPanelViewModel(ViewModel):
    """The dev deploy of a ticket's merge, as a graph of its jobs."""
    template = "_deploy.html"
    deploy: dict | None = None

    @classmethod
    def load(cls, container, key: str) -> "DeployPanelViewModel":
        settings = container.settings()
        with container.session() as s:
            return cls(deploy=s.deploy_graph(settings).dev_deploy(key))
