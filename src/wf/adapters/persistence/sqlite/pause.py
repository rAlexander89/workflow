"""SQLite pause switch, one row in the board's database."""

from __future__ import annotations

import sqlite3

SCHEMA = """
create table if not exists work_pause (
    id         integer primary key check (id = 1),
    paused     integer not null default 0,
    paused_at  integer,
    resumed_at integer,
    reason     text
);
"""


class SqlitePauseState:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        conn.executescript(SCHEMA)

    def is_paused(self) -> bool:
        row = self._conn.execute("select paused from work_pause where id = 1").fetchone()
        return bool(row) and bool(row[0])

    def paused_since(self) -> int | None:
        row = self._conn.execute("select paused_at from work_pause where id = 1 and paused = 1").fetchone()
        return row[0] if row else None

    def pause(self, reason: str, now: int) -> bool:
        if self.is_paused():
            return False
        self._conn.execute(
            "insert into work_pause (id, paused, paused_at, reason) values (1, 1, ?, ?) "
            "on conflict(id) do update set paused = 1, paused_at = excluded.paused_at, reason = excluded.reason",
            (now, reason))
        self._conn.commit()
        return True

    def resume(self, now: int) -> bool:
        if not self.is_paused():
            return False
        self._conn.execute("update work_pause set paused = 0, resumed_at = ?, reason = null where id = 1", (now,))
        self._conn.commit()
        return True
