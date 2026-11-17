"""Supervisor: routes each ticket status change to the agent that owns its stage.

A Jira status is mapped to an internal stage (see `wf.mapping`), and the stage has an
owner. Both steps are fixed rules, not a model call. A status with no stage is
"unknown" and nothing is dispatched. Every decision is written to the
local lifecycle record. A kill switch halts all dispatch, and a loop guard
escalates a ticket that keeps re-entering the same status.

Agents are not built yet. The dispatch callable is injected, so until agents
exist the supervisor records the handoff and does nothing else.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from wf.events import StatusChange

from wf.adapters.persistence.sqlite.lifecycle import SCHEMA, SqliteLifecycleLog as LifecycleStore  # noqa: F401
from wf.domain.delivery.routing import (  # noqa: F401
    HOTFIX_LABEL, MAX_DISPATCHES_PER_STATUS, STAGE_OWNERS, Decision, route,
)

Dispatch = Callable[[str, str, str], bool]  # (owner, issue_key, status) -> dispatched?


def no_agents_wired(owner: str, issue_key: str, status: str) -> bool:
    """Placeholder until the agents exist: nothing runs, nothing is dispatched."""
    return False


def board_status_to_stage(path: Path) -> dict[str, str]:
    """The stage mapping from the board's SQLite file. Empty until a sync has run."""
    from wf import board

    conn = board.connect(Path(path))
    try:
        return board.status_to_stage(conn)
    finally:
        conn.close()


class Supervisor:
    def __init__(
        self,
        store: LifecycleStore,
        dispatch: Dispatch = no_agents_wired,
        max_dispatches_per_status: int = MAX_DISPATCHES_PER_STATUS,
        status_to_stage: Callable[[], Mapping[str, str]] | None = None,
    ):
        self.store = store
        self._dispatch = dispatch
        self._max = max_dispatches_per_status
        self._status_to_stage = status_to_stage or (lambda: board_status_to_stage(store.path))

    def handle(self, change: StatusChange, labels: set[str] | frozenset[str] = frozenset()) -> Decision:
        if self.store.killed():
            decision = Decision("halt", None, "kill switch is on: nothing is dispatched")
            self.store.record(change, decision, dispatched=False)
            return decision

        decision = route(change.to_status, labels, self._status_to_stage())

        if decision.action in ("dispatch", "merge"):
            if self.store.dispatch_count(change.issue_key, change.to_status) >= self._max:
                decision = Decision(
                    "escalate",
                    None,
                    f"{change.to_status!r} reached {self._max} times: needs a human",
                )

        dispatched = False
        if decision.action in ("dispatch", "merge"):
            dispatched = bool(self._dispatch(decision.owner, change.issue_key, change.to_status))
        self.store.record(change, decision, dispatched=dispatched)
        return decision
