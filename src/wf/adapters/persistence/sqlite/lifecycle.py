"""SQLite lifecycle record: every routing decision, and the kill switch."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from wf.domain.delivery.routing import Decision
from wf.domain.tracker.changelog import StatusChange

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


class SqliteLifecycleLog:
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

    def history(self, issue_key: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "select * from lifecycle where issue_key = ? order by id", (issue_key,)
            ).fetchall()
        return [dict(r) for r in rows]

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
