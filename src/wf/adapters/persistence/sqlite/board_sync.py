"""Applies a validated sync payload to the board's SQLite tables, in one transaction."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from wf.domain.board import stages as mapping
from wf.domain.board.errors import BoardError
from wf.domain.board.payload import SyncResult, _list, validate

def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [])


def _stage_for(conn: sqlite3.Connection, project_key: str, status: str) -> str | None:
    """The stage a ticket is in, from its Jira status.

    The mapping keeps one status per stage (the one used to move a ticket there). A ticket can be
    in any status whose name points to exactly one stage, so "Open" and "Backlog" are both "To do".
    A status that points to several stages is left out, as before.
    """
    row = conn.execute(
        "select stage from stage_mapping where project_key = ? and status_name = ?",
        (project_key, status),
    ).fetchone()
    if row:
        return row["stage"]
    from wf import mapping
    stages = mapping.stages_for(status)
    return stages[0] if len(stages) == 1 else None


def _apply_projects(conn: sqlite3.Connection, data: dict, now: int, result: SyncResult) -> None:
    for p in _list(data, "projects"):
        conn.execute(
            "insert into project (key, name, discovered_at) values (?, ?, ?) "
            "on conflict(key) do update set name = excluded.name",
            (p["key"], p["name"], now),
        )
        statuses = _list(p, "statuses")
        conn.execute("delete from stage_mapping where project_key = ?", (p["key"],))
        conn.execute("delete from project_status where project_key = ?", (p["key"],))
        for s in statuses:
            conn.execute(
                "insert into project_status (project_key, name, category, source) values (?, ?, ?, ?)",
                (p["key"], s["name"], s["category"], s.get("source", "observed")),
            )
        if statuses:
            rows, problems = mapping.map_project([s["name"] for s in statuses])
            for row in rows:
                conn.execute(
                    "insert into stage_mapping (project_key, stage, status_name, reason) values (?, ?, ?, ?)",
                    (p["key"], row.stage, row.status_name, row.reason),
                )
            result.problems.extend(f"{p['key']}: {msg}" for msg in problems)
            result.problems.extend(
                f"{p['key']}: stage {row.stage!r} is unmapped ({row.reason})"
                for row in rows if row.status_name is None
            )
        result.bump("projects")


def _apply_sprints(conn: sqlite3.Connection, data: dict, now: int, result: SyncResult) -> None:
    for s in _list(data, "sprints"):
        old = conn.execute("select state from sprint where id = ?", (s["id"],)).fetchone()
        conn.execute(
            "insert into sprint (id, board_id, name, state, goal, start_date, end_date, complete_date, synced_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "on conflict(id) do update set board_id = excluded.board_id, name = excluded.name, "
            "state = excluded.state, goal = excluded.goal, start_date = excluded.start_date, "
            "end_date = excluded.end_date, complete_date = excluded.complete_date, synced_at = excluded.synced_at",
            (s["id"], s["board_id"], s["name"], s["state"], s.get("goal"), s.get("start_date"),
             s.get("end_date"), s.get("complete_date"), now),
        )
        if old is None or old["state"] != s["state"]:
            conn.execute(
                "insert into sprint_event (sprint_id, at, from_state, to_state, source) values (?, ?, ?, ?, 'sync')",
                (s["id"], now, old["state"] if old else None, s["state"]),
            )
            result.bump("sprint_changes")
        result.bump("sprints")


def _event(conn, key, now, actor, kind, from_value, to_value, detail=None) -> None:
    conn.execute(
        "insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
        "values (?, ?, ?, ?, ?, ?, ?)",
        (key, now, actor, kind, None if from_value is None else str(from_value),
         None if to_value is None else str(to_value), detail),
    )


def _apply_tickets(conn: sqlite3.Connection, data: dict, kind: str, now: int, result: SyncResult) -> set[str]:
    seen: set[str] = set()
    for t in _list(data, "tickets"):
        key = t["key"]
        seen.add(key)
        existing = conn.execute("select * from ticket where key = ?", (key,)).fetchone()
        # A past payload is a snapshot of closed sprints and can be stale, so it must not
        # overwrite a ticket that is now in an active sprint. A backlog payload is current:
        # a ticket in it has no sprint, so it is applied even if the row shows a sprint.
        if kind == "past" and existing is not None and _sprint_is_active(conn, existing["sprint_id"]):
            result.bump("skipped_active")
            continue

        status = t["status"]
        stage = _stage_for(conn, t["project_key"], status)
        labels = t.get("labels", [])
        sprint_id = t.get("sprint_id")

        if existing is None:
            conn.execute(
                "insert into ticket (key, project_key, jira_id, summary, description, acceptance, issue_type, "
                "priority, points, assignee_id, jira_status, jira_status_category, labels, sprint_id, stage, "
                "flags, jira_updated, synced_at, removed_at, assignee_name) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, null, ?)",
                (key, t["project_key"], str(t["jira_id"]), t["summary"], t.get("description"),
                 t.get("acceptance"), t.get("issue_type"), t.get("priority"), t.get("points"),
                 t.get("assignee_id"), status, t.get("status_category"), _json(labels), sprint_id,
                 stage, t.get("updated"), now, t.get("assignee_name")),
            )
            _event(conn, key, now, "sync", "sync", None, sprint_id, "created")
            result.bump("tickets_created")
            continue

        _update_ticket(conn, existing, t, status, stage, labels, sprint_id, now, result)

    return seen


def _update_ticket(conn, existing, t, status, stage, labels, sprint_id, now, result) -> None:
    key = existing["key"]
    status_changed = existing["jira_status"] != status
    sprint_changed = existing["sprint_id"] != sprint_id
    new_stage = existing["stage"]
    if status_changed and stage is not None:
        new_stage = stage  # a Jira-side move is mirrored onto the board
    elif existing["stage"] is None and stage is not None:
        new_stage = stage

    conn.execute(
        "update ticket set summary = ?, description = ?, acceptance = ?, issue_type = ?, priority = ?, "
        "points = ?, assignee_id = ?, jira_status = ?, jira_status_category = ?, labels = ?, "
        "sprint_id = ?, stage = ?, jira_updated = ?, synced_at = ?, removed_at = null, assignee_name = ? "
        "where key = ?",
        (t["summary"], t.get("description"), t.get("acceptance"), t.get("issue_type"), t.get("priority"),
         t.get("points"), t.get("assignee_id"), status, t.get("status_category"), _json(labels),
         sprint_id, new_stage, t.get("updated"), now, t.get("assignee_name"), key),
    )
    if status_changed:
        _event(conn, key, now, "sync", "status", existing["jira_status"], status)
        result.bump("status_changes")
    if sprint_changed:
        _event(conn, key, now, "sync", "sprint", existing["sprint_id"], sprint_id)
        result.bump("sprint_changes_tickets")
    if new_stage != existing["stage"]:
        _event(conn, key, now, "sync", "stage", existing["stage"], new_stage, "mirrored from Jira status")
    if existing["removed_at"] is not None:
        _event(conn, key, now, "sync", "sync", None, sprint_id, "restored")
        result.bump("restored")
    result.bump("tickets_updated")


def _sprint_is_active(conn, sprint_id: str | None) -> bool:
    if sprint_id is None:
        return False
    row = conn.execute("select state from sprint where id = ?", (sprint_id,)).fetchone()
    return row is not None and row["state"] == "active"


def _remove_missing(conn, keys, seen: set[str], now: int, result: SyncResult, reason: str, still_in) -> None:
    """Mark tickets removed when a sync no longer returns them. Rows and history are kept."""
    for key in keys:
        row = conn.execute("select * from ticket where key = ?", (key,)).fetchone()
        if row is None or row["removed_at"] is not None:
            continue
        if key in seen and still_in(row):
            continue
        conn.execute("update ticket set removed_at = ? where key = ?", (now, key))
        _event(conn, key, now, "sync", "sync", row["sprint_id"], None, reason)
        result.bump("removed")


def _apply_links(conn, data: dict, result: SyncResult) -> None:
    """Replace each synced ticket's links with the ones Jira reports now."""
    for t in _list(data, "tickets"):
        if "links" not in t or conn.execute("select 1 from ticket where key = ?", (t["key"],)).fetchone() is None:
            continue
        conn.execute("delete from ticket_link where ticket_key = ? and synced = 1", (t["key"],))
        for link in t.get("links") or []:
            conn.execute(
                "insert or replace into ticket_link (ticket_key, other_key, link_type, direction, other_summary, "
                "other_status) values (?, ?, ?, ?, ?, ?)",
                (t["key"], link["key"], link.get("type") or "related", link.get("direction") or "",
                 link.get("summary"), link.get("status")))
            result.bump("links")


def _apply_comments(conn, data: dict, now: int, result: SyncResult) -> None:
    for c in _list(data, "comments"):
        if conn.execute("select 1 from ticket where key = ?", (c["ticket_key"],)).fetchone() is None:
            result.bump("comments_skipped_unknown_ticket")
            continue
        conn.execute(
            "insert into ticket_comment (ticket_key, origin, jira_comment_id, author, body, created_at, updated_at, synced_at) "
            "values (?, 'jira', ?, ?, ?, ?, ?, ?) "
            "on conflict(jira_comment_id) do update set author = excluded.author, body = excluded.body, "
            "updated_at = excluded.updated_at, synced_at = excluded.synced_at",
            (c["ticket_key"], str(c["jira_comment_id"]), c["author"], c["body"], c["created"],
             c.get("updated"), now),
        )
        result.bump("comments")


def apply_payload(conn: sqlite3.Connection, data: dict, clock: Callable[[], float] = time.time) -> SyncResult:
    """Validate and apply one sync payload. Atomic: a failure changes nothing."""
    data = validate(data)
    kind = data["kind"]
    now = int(clock())
    result = SyncResult()

    cur = conn.execute("insert into sync_run (started_at, kind, status) values (?, ?, 'running')", (now, kind))
    run_id = cur.lastrowid
    conn.commit()

    try:
        with conn:
            active_before = {
                r["key"] for r in conn.execute(
                    "select t.key from ticket t join sprint s on s.id = t.sprint_id "
                    "where s.state = 'active' and t.removed_at is null"
                )
            }
            backlog_before = {
                r["key"] for r in conn.execute(
                    "select key from ticket where sprint_id is null and removed_at is null"
                )
            }
            _apply_projects(conn, data, now, result)
            _apply_sprints(conn, data, now, result)
            seen = _apply_tickets(conn, data, kind, now, result)

            if kind == "active":
                _remove_missing(conn, sorted(active_before), seen, now, result, "left the active sprint",
                                still_in=lambda row: _sprint_is_active(conn, row["sprint_id"]))
            elif kind == "backlog":
                _remove_missing(conn, sorted(backlog_before), seen, now, result, "left the backlog",
                                still_in=lambda row: row["sprint_id"] is None)

            _apply_comments(conn, data, now, result)
            _apply_links(conn, data, result)
            conn.execute(
                "update sync_run set finished_at = ?, status = 'ok', counts = ? where id = ?",
                (int(clock()), json.dumps(result.counts), run_id),
            )
    except Exception as e:
        conn.execute(
            "update sync_run set finished_at = ?, status = 'failed', error = ? where id = ?",
            (int(clock()), str(e)[:500], run_id),
        )
        conn.commit()
        raise BoardError(f"sync failed and was rolled back: {e}") from e
    return result


def sync_file(conn: sqlite3.Connection, path: Path, clock: Callable[[], float] = time.time) -> SyncResult:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BoardError(f"could not read {path}: {e}") from e
    return apply_payload(conn, data, clock)


def carry_workflow_statuses(conn: sqlite3.Connection, data: dict) -> dict:
    """Keep the project's workflow statuses from the last sync.

    A session can only report statuses that some ticket has, so a status with no tickets
    (for example an empty board column) is missing from the payload. Those statuses come
    from the project's workflow, which doesn't change between syncs, so carry them forward.
    """
    for p in data.get("projects", []):
        have = {s["name"] for s in p.get("statuses", [])}
        for row in conn.execute(
            "select name, category from project_status where project_key = ? and source = 'workflow'",
            (p["key"],)).fetchall():
            if row["name"] not in have:
                p.setdefault("statuses", []).append(
                    {"name": row["name"], "category": row["category"], "source": "workflow"})
    return data


class SqliteBoardSync:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def apply(self, data: dict, clock: Callable[[], float]) -> SyncResult:
        return apply_payload(self._conn, data, clock)
