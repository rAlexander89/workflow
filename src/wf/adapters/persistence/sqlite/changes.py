"""SQLite record of the Jira status changes already seen, and when the last webhook arrived."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable

from wf.domain.tracker.changelog import StatusChange

SCHEMA = """
create table if not exists processed_changes (
    issue_key text not null,
    history_id text not null,
    from_status text not null,
    to_status text not null,
    created text not null,
    seen_at integer not null,
    primary key (issue_key, history_id)
);
create table if not exists meta (
    key text primary key,
    value text not null
);
"""


class SqliteChangeLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), check_same_thread=False)

    def record_new(self, changes: Iterable[StatusChange]) -> list[StatusChange]:
        """Store each change once. Returns only the ones not seen before."""
        new: list[StatusChange] = []
        seen_at = int(self._clock())
        with closing(self._connect()) as conn, conn:
            for c in changes:
                cur = conn.execute(
                    "insert or ignore into processed_changes "
                    "(issue_key, history_id, from_status, to_status, created, seen_at) "
                    "values (?, ?, ?, ?, ?, ?)",
                    (c.issue_key, c.history_id, c.from_status, c.to_status, c.created, seen_at),
                )
                if cur.rowcount == 1:
                    new.append(c)
        return new

    def last_webhook_at(self) -> float | None:
        with closing(self._connect()) as conn:
            row = conn.execute("select value from meta where key = 'last_webhook_at'").fetchone()
        return float(row[0]) if row else None

    def mark_webhook(self, when: float | None = None) -> None:
        value = str(self._clock() if when is None else when)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "insert into meta (key, value) values ('last_webhook_at', ?) "
                "on conflict(key) do update set value = excluded.value",
                (value,),
            )
