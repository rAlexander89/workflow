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

from wf.adapters.persistence.sqlite.pause import SCHEMA, SqlitePauseState  # noqa: F401


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def is_paused(conn: sqlite3.Connection) -> bool:
    return SqlitePauseState(conn).is_paused()


def paused_since(conn: sqlite3.Connection) -> int | None:
    return SqlitePauseState(conn).paused_since()


def pause(conn: sqlite3.Connection, reason: str = "paused by a person",
          clock: Callable[[], float] = time.time) -> bool:
    """Turn the pause on. Returns False if work was already paused."""
    return SqlitePauseState(conn).pause(reason, int(clock()))


def resume(conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> bool:
    """Turn the pause off. Returns False if work was not paused."""
    return SqlitePauseState(conn).resume(int(clock()))
