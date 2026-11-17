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

HOTFIX_LABEL = "hotfix"
MAX_DISPATCHES_PER_STATUS = 10  # loop guard; a ticket bouncing this often needs a human

# Owner per internal stage. None = no agent acts in this stage alone.
#   "product"       the Product agent
#   "planning"      Product and Staff, with the user (refinement)
#   "dev"           the Dev agent
#   "pr_review"     the PR review agent
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
    "ready_for_qa": "qa",
    "qa_approved": "harness:merge",
    "merged": None,  # CI deploys the internal main to the dev environment
    "regression": "qa",
    "deployed": None,  # the performance agent picks these up later
}

SCHEMA = """
create table if not exists lifecycle (
    id integer primary key autoincrement,
    created_at integer not null,
    issue_key text not null,
    from_status text not null,
    to_status text not null,
    action text not null,
    owner text,
    reason text not null,
    dispatched integer not null
);
create index if not exists lifecycle_issue on lifecycle (issue_key, to_status);
create table if not exists meta (
    key text primary key,
    value text not null
);
"""


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


class LifecycleStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, change: StatusChange, decision: Decision, dispatched: bool) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "insert into lifecycle (created_at, issue_key, from_status, to_status, action, owner, reason, dispatched) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(self._clock()),
                    change.issue_key,
                    change.from_status,
                    change.to_status,
                    decision.action,
                    decision.owner,
                    decision.reason,
                    int(dispatched),
                ),
            )

    def dispatch_count(self, issue_key: str, status: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "select count(*) from lifecycle where issue_key = ? and to_status = ? and dispatched = 1",
                (issue_key, status),
            ).fetchone()
        return row[0]

    def history(self, issue_key: str) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "select * from lifecycle where issue_key = ? order by id", (issue_key,)
            ).fetchall()

    def set_kill(self, on: bool) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "insert into meta (key, value) values ('kill', ?) on conflict(key) do update set value = excluded.value",
                ("on" if on else "off",),
            )

    def killed(self) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("select value from meta where key = 'kill'").fetchone()
        return bool(row) and row[0] == "on"


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
