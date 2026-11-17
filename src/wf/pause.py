"""Pause all agent work, and resume it later.

Pausing is a flag in the board's SQLite file. While it is on, nothing new starts: no agent
run, no stage move made by the harness, no merge, no GitHub publish. A run already in
progress is allowed to finish, so its result is recorded and its worktree is left clean.

The work itself is already saved: each ticket's stage, flags, run history, and worktree live
in the board and the repo, so resuming picks each ticket up at the stage it was in.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

SCHEMA = """
create table if not exists work_pause (
    id         integer primary key check (id = 1),
    paused     integer not null default 0,
    paused_at  integer,
    resumed_at integer,
    reason     text
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def is_paused(conn: sqlite3.Connection) -> bool:
    ensure_schema(conn)
    row = conn.execute("select paused from work_pause where id = 1").fetchone()
    return bool(row) and bool(row[0])


def paused_since(conn: sqlite3.Connection) -> int | None:
    ensure_schema(conn)
    row = conn.execute("select paused_at from work_pause where id = 1 and paused = 1").fetchone()
    return row[0] if row else None


def pause(conn: sqlite3.Connection, reason: str = "paused by a person",
          clock: Callable[[], float] = time.time) -> bool:
    """Turn the pause on. Returns False if work was already paused."""
    ensure_schema(conn)
    if is_paused(conn):
        return False
    now = int(clock())
    conn.execute(
        "insert into work_pause (id, paused, paused_at, reason) values (1, 1, ?, ?) "
        "on conflict(id) do update set paused = 1, paused_at = excluded.paused_at, reason = excluded.reason",
        (now, reason))
    conn.commit()
    return True


def resume(conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> bool:
    """Turn the pause off. Returns False if work was not paused."""
    ensure_schema(conn)
    if not is_paused(conn):
        return False
    conn.execute("update work_pause set paused = 0, resumed_at = ?, reason = null where id = 1", (int(clock()),))
    conn.commit()
    return True
