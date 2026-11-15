"""Supervisor: routes each ticket status change to the agent that owns that status.

Routing is a fixed table, not a model call. Every decision is written to the
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
from typing import Callable

from wf.events import StatusChange

HOTFIX_LABEL = "hotfix"
MAX_DISPATCHES_PER_STATUS = 10  # loop guard; a ticket bouncing this often needs a human

# Owner per status. None = no agent acts on this status alone.
#   "planning"      Product and Staff, with the user (refinement)
#   "harness:merge" the harness merges to main once both approvals exist
ROUTES: dict[str, str | None] = {
    "Backlog": None,
    "Needs refinement": "product",
    "In refinement": "planning",
    "Hotfix review": "planning",
    "Ready for dev": "dev",
    "Dev in progress": "dev",
    "PR open": "pr_review",
    "PR in review": "pr_review",
    "Ready for QA": "qa",
    "QA approved": "harness:merge",
    "Merged to main": None,  # CI deploys main to the dev environment
    "Regression testing": "qa",
    "Deployed to prod": None,  # the performance agent picks these up at sprint end
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


def route(status: str, labels: set[str] | frozenset[str] = frozenset()) -> Decision:
    if status not in ROUTES:
        return Decision("unknown", None, f"no route for status {status!r}")
    owner = ROUTES[status]
    if owner is None:
        return Decision("none", None, f"{status!r} has no owning agent")

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


class Supervisor:
    def __init__(
        self,
        store: LifecycleStore,
        dispatch: Dispatch = no_agents_wired,
        max_dispatches_per_status: int = MAX_DISPATCHES_PER_STATUS,
    ):
        self.store = store
        self._dispatch = dispatch
        self._max = max_dispatches_per_status

    def handle(self, change: StatusChange, labels: set[str] | frozenset[str] = frozenset()) -> Decision:
        if self.store.killed():
            decision = Decision("halt", None, "kill switch is on: nothing is dispatched")
            self.store.record(change, decision, dispatched=False)
            return decision

        decision = route(change.to_status, labels)

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
