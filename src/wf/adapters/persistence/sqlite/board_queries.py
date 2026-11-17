"""Read-side queries over the board's SQLite tables."""

from __future__ import annotations

import sqlite3

from wf.domain.board import stages as mapping

# --- reads ----------------------------------------------------------------------

def status_to_stage(conn: sqlite3.Connection) -> dict[str, str]:
    """Jira status name -> internal stage, for routing. Names that map to different
    stages in different projects are left out, because routing can't tell them apart."""
    seen: dict[str, set[str]] = {}
    for r in conn.execute("select status_name, stage from stage_mapping where status_name is not null"):
        if r["stage"] not in mapping.STAGES:  # a stage that no longer exists routes nothing
            continue
        seen.setdefault(r["status_name"], set()).add(r["stage"])
    return {name: next(iter(stages)) for name, stages in seen.items() if len(stages) == 1}


def active_board(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "select * from active_sprint_tickets order by stage is null, stage, key"
    ).fetchall()]


def backlog(conn: sqlite3.Connection) -> list[dict]:
    """Tickets in no sprint: the signed-in user's backlog, as of the last backlog sync."""
    return [dict(r) for r in conn.execute(
        "select * from backlog_tickets order by stage is null, stage, key"
    ).fetchall()]


def unmapped_stages(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "select project_key, stage, reason from stage_mapping where status_name is null order by project_key, stage"
    ).fetchall()]


def sprints(conn: sqlite3.Connection, state: str | None = None) -> list[dict]:
    if state is None:
        return [dict(r) for r in conn.execute("select * from sprint order by state, start_date, id").fetchall()]
    return [dict(r) for r in conn.execute(
        "select * from sprint where state = ? order by start_date, id", (state,)).fetchall()]


def ticket_detail(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("select * from ticket where key = ?", (key,)).fetchone()
    if row is None:
        return None
    return {
        "ticket": dict(row),
        "comments": [dict(r) for r in conn.execute(
            "select * from ticket_comment where ticket_key = ? order by created_at, id", (key,))],
        "events": [dict(r) for r in conn.execute(
            "select * from ticket_event where ticket_key = ? order by at, id", (key,))],
    }


def last_sync(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("select * from sync_run order by id desc limit 1").fetchone()
    return None if row is None else dict(row)


def counts(conn: sqlite3.Connection) -> dict:
    """Tickets in scope, flagged tickets, and unmapped stages."""
    return {
        "active_tickets": conn.execute("select count(*) from active_sprint_tickets").fetchone()[0],
        "backlog_tickets": conn.execute("select count(*) from backlog_tickets").fetchone()[0],
        "flagged": [dict(r) for r in conn.execute(
            "select key, flags from ticket where flags != '[]' and removed_at is null order by key")],
        "unmapped_stages": unmapped_stages(conn),
    }


class SqliteBoardQueries:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def status_to_stage(self) -> dict[str, str]:
        return status_to_stage(self._conn)

    def active_board(self) -> list[dict]:
        return active_board(self._conn)

    def backlog(self) -> list[dict]:
        return backlog(self._conn)

    def unmapped_stages(self) -> list[dict]:
        return unmapped_stages(self._conn)

    def sprints(self, state: str | None = None) -> list[dict]:
        return sprints(self._conn, state)

    def ticket_detail(self, key: str) -> dict | None:
        return ticket_detail(self._conn, key)

    def last_sync(self) -> dict | None:
        return last_sync(self._conn)

    def counts(self) -> dict:
        return counts(self._conn)
