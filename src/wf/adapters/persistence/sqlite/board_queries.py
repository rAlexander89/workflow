"""Read-side queries over the board's SQLite tables."""

from __future__ import annotations

import json
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


def last_sync(conn: sqlite3.Connection, kind: str | None = None) -> dict | None:
    if kind is None:
        row = conn.execute("select * from sync_run order by id desc limit 1").fetchone()
    else:
        row = conn.execute("select * from sync_run where kind = ? order by id desc limit 1", (kind,)).fetchone()
    return None if row is None else dict(row)


def active_sprint(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("select id, name from sprint where state = 'active' order by id limit 1").fetchone()
    return None if row is None else dict(row)


def sprint_name(conn: sqlite3.Connection, sprint_id: str) -> str | None:
    row = conn.execute("select name from sprint where id = ?", (sprint_id,)).fetchone()
    return None if row is None else row["name"]


def ticket_links(conn: sqlite3.Connection, key: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "select * from ticket_link where ticket_key = ? order by link_type, other_key", (key,))]


def counts(conn: sqlite3.Connection) -> dict:
    """Tickets in scope, flagged tickets, and unmapped stages."""
    return {
        "active_tickets": conn.execute("select count(*) from active_sprint_tickets").fetchone()[0],
        "backlog_tickets": conn.execute("select count(*) from backlog_tickets").fetchone()[0],
        "flagged": [dict(r) for r in conn.execute(
            "select key, flags from ticket where flags != '[]' and removed_at is null order by key")],
        "unmapped_stages": unmapped_stages(conn),
    }


def _pipeline(row: sqlite3.Row) -> dict:
    return {"key": row["key"], "summary": row["summary"], "stage": row["stage"],
            "flags": json.loads(row["flags"] or "[]"), "sort_order": row["sort_order"],
            "sprint_state": row["sprint_state"]}


def pipeline_rows(conn: sqlite3.Connection) -> list[dict]:
    return [_pipeline(r) for r in conn.execute(
        """select t.key, t.summary, t.stage, t.flags, t.sort_order, s.state as sprint_state
             from ticket t left join sprint s on s.id = t.sprint_id
            where t.removed_at is null and t.stage is not null""").fetchall()]


def pipeline_row(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "select t.key, t.summary, t.stage, t.flags, t.sort_order, s.state as sprint_state from ticket t "
        "left join sprint s on s.id = t.sprint_id where t.key = ? and t.removed_at is null", (key,)).fetchone()
    return None if row is None else _pipeline(row)


def sprint_backlog_keys(conn: sqlite3.Connection) -> list[str]:
    return [r["key"] for r in conn.execute(
        "select t.key from ticket t join sprint s on s.id = t.sprint_id "
        "where t.stage = 'backlog' and s.state = 'active' and t.removed_at is null order by t.key").fetchall()]


def kicked_back_dependents(conn: sqlite3.Connection, stages: tuple[str, ...]) -> list[dict]:
    marks = ", ".join("?" for _ in stages)
    return [dict(r) for r in conn.execute(
        "select distinct t.key, b.key as blocker from ticket t "
        "join sprint s on s.id = t.sprint_id "
        "join ticket_link l on l.ticket_key = t.key and l.link_type = 'is blocked by' "
        "join ticket b on b.key = l.other_key "
        f"where s.state = 'active' and t.removed_at is null and t.stage in ({marks}) "
        "and b.stage = 'in_dev' order by t.key", stages).fetchall()]


def keys_in_stages(conn: sqlite3.Connection, stages: tuple[str, ...]) -> list[dict]:
    marks = ", ".join("?" for _ in stages)
    return [dict(r) for r in conn.execute(
        f"select key, stage from ticket where stage in ({marks}) and removed_at is null", stages).fetchall()]


def stage_by_key(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["stage"] for r in conn.execute("select key, stage from ticket where removed_at is null")}


def qa_approved_without_pr(conn: sqlite3.Connection) -> list[str]:
    return [r["key"] for r in conn.execute(
        "select key from ticket where stage = 'qa_approved' and removed_at is null "
        "and key not in (select ticket_key from ticket_event where kind = 'pr')").fetchall()]


def ready_candidates(conn: sqlite3.Connection, stage: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """select t.*, s.name as sprint_name, s.state as sprint_state
             from ticket t left join sprint s on s.id = t.sprint_id
            where t.stage = ? and t.removed_at is null
            order by t.key""", (stage,)).fetchall()]


def database_path(conn: sqlite3.Connection) -> str:
    return str(conn.execute("pragma database_list").fetchone()[2])


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

    def last_sync(self, kind: str | None = None) -> dict | None:
        return last_sync(self._conn, kind)

    def active_sprint(self) -> dict | None:
        return active_sprint(self._conn)

    def sprint_name(self, sprint_id: str) -> str | None:
        return sprint_name(self._conn, sprint_id)

    def ticket_links(self, key: str) -> list[dict]:
        return ticket_links(self._conn, key)

    def counts(self) -> dict:
        return counts(self._conn)

    def pipeline_rows(self) -> list[dict]:
        return pipeline_rows(self._conn)

    def pipeline_row(self, key: str) -> dict | None:
        return pipeline_row(self._conn, key)

    def sprint_backlog_keys(self) -> list[str]:
        return sprint_backlog_keys(self._conn)

    def kicked_back_dependents(self, stages: tuple[str, ...]) -> list[dict]:
        return kicked_back_dependents(self._conn, stages)

    def keys_in_stages(self, stages: tuple[str, ...]) -> list[dict]:
        return keys_in_stages(self._conn, stages)

    def stage_by_key(self) -> dict[str, str]:
        return stage_by_key(self._conn)

    def qa_approved_without_pr(self) -> list[str]:
        return qa_approved_without_pr(self._conn)

    def ready_candidates(self, stage: str) -> list[dict]:
        return ready_candidates(self._conn, stage)

    def database_path(self) -> str:
        return database_path(self._conn)
