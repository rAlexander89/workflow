"""The local sprint board: sprints, tickets, comments, and their history in SQLite.

Jira is read-only. The Atlassian MCP lives in the user's Claude session, so the session
runs the queries and passes the results here as a JSON payload (see docs/sync-format.md).
This module validates the payload and applies it.

Sync rules:
- A "active" sync covers the signed-in user's tickets in active sprints. A ticket that
  was in an active sprint and is missing from the payload, or has moved out of active
  sprints, is marked removed. Its row and history are kept.
- A "past" sync covers tickets from closed sprints, fetched on demand. It never
  overwrites a ticket whose current sprint is active.
- A "backlog" sync covers the signed-in user's tickets that are in no sprint and not
  done. It is current Jira state, so a ticket it returns is applied even if it was in a
  sprint before (it has been taken out). A backlog ticket missing from the payload is
  marked removed.
- Jira status is mirrored. A status change in Jira moves the local stage to the stage
  the new status maps to. Otherwise the local stage is kept, so agent moves stick.
- Every change is recorded in ticket_event and sprint_event. Nothing is deleted.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from wf import mapping

SCHEMA_VERSION = 1

SCHEMA = """
pragma foreign_keys = on;

create table if not exists project (
    key           text primary key,
    name          text not null,
    discovered_at integer not null
);

create table if not exists project_status (
    project_key text not null references project(key),
    name        text not null,
    category    text not null check (category in ('new', 'indeterminate', 'done')),
    source      text not null check (source in ('observed', 'workflow')),
    primary key (project_key, name)
);

create table if not exists stage_mapping (
    project_key text not null references project(key),
    stage       text not null,
    status_name text,
    reason      text not null,
    primary key (project_key, stage),
    foreign key (project_key, status_name) references project_status(project_key, name)
);

create table if not exists sprint (
    id            text primary key,
    board_id      text not null,
    name          text not null,
    state         text not null check (state in ('future', 'active', 'closed')),
    goal          text,
    start_date    integer,
    end_date      integer,
    complete_date integer,
    synced_at     integer not null
);
create index if not exists sprint_state on sprint (state);

create table if not exists sprint_event (
    id         integer primary key autoincrement,
    sprint_id  text not null references sprint(id),
    at         integer not null,
    from_state text,
    to_state   text not null,
    source     text not null check (source in ('sync', 'user'))
);

create table if not exists ticket (
    key                  text primary key,
    project_key          text not null references project(key),
    jira_id              text not null,
    summary              text not null,
    description          text,
    acceptance           text,
    issue_type           text,
    priority             text,
    points               real,
    assignee_id          text,
    jira_status          text not null,
    jira_status_category text,
    labels               text not null default '[]',
    sprint_id            text references sprint(id),
    stage                text,
    flags                text not null default '[]',
    jira_updated         integer,
    synced_at            integer not null,
    removed_at           integer
);
create index if not exists ticket_stage on ticket (stage);
create index if not exists ticket_project on ticket (project_key);
create index if not exists ticket_sprint_id on ticket (sprint_id);

create table if not exists ticket_event (
    id         integer primary key autoincrement,
    ticket_key text not null references ticket(key),
    at         integer not null,
    actor      text not null,
    kind       text not null,
    from_value text,
    to_value   text,
    detail     text
);
create index if not exists ticket_event_key on ticket_event (ticket_key, at);

create table if not exists ticket_comment (
    id              integer primary key autoincrement,
    ticket_key      text not null references ticket(key),
    origin          text not null check (origin in ('jira', 'user', 'agent')),
    jira_comment_id text unique,
    author          text not null,
    body            text not null,
    created_at      integer not null,
    updated_at      integer,
    synced_at       integer
);
create index if not exists ticket_comment_key on ticket_comment (ticket_key, created_at);

create table if not exists sync_run (
    id          integer primary key autoincrement,
    started_at  integer not null,
    finished_at integer,
    kind        text not null check (kind in ('active', 'past', 'backlog')),
    status      text not null check (status in ('running', 'ok', 'failed')),
    counts      text,
    error       text
);

-- Changes waiting to be pushed to Jira by the Jira agent. Each row is sent at most once.
create table if not exists outbox (
    id          integer primary key autoincrement,
    created_at  integer not null,
    ticket_key  text not null references ticket(key),
    kind        text not null check (kind in ('transition')),
    target_stage text not null,
    actor       text not null,
    status      text not null check (status in
                ('pending', 'sending', 'sent', 'skipped', 'blocked', 'conflict', 'failed', 'superseded')),
    attempts    integer not null default 0,
    detail      text,
    jira_response text,
    updated_at  integer
);
create index if not exists outbox_status on outbox (status);
create index if not exists outbox_ticket on outbox (ticket_key, status);

create view if not exists active_sprint_tickets as
select t.*, s.name as sprint_name
from ticket t
join sprint s on s.id = t.sprint_id
where s.state = 'active' and t.removed_at is null;

create view if not exists past_sprints as
select * from sprint where state = 'closed';

create view if not exists backlog_tickets as
select * from ticket where sprint_id is null and removed_at is null;
"""

SPRINT_STATES = ("future", "active", "closed")
STATUS_CATEGORIES = ("new", "indeterminate", "done")
SYNC_KINDS = ("active", "past", "backlog")


class BoardError(ValueError):
    pass


# --- connection and schema -----------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.executescript(SCHEMA)
    conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
    return conn


# --- payload validation ---------------------------------------------------------

def _need(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise BoardError(f"{where}: missing {key!r}")
    return obj[key]


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise BoardError(f"{key!r} must be a list")
    return value


def validate(data: Any) -> dict:
    """Check the payload's shape. Returns it unchanged on success."""
    if not isinstance(data, dict):
        raise BoardError("payload must be a JSON object")
    kind = _need(data, "kind", "payload")
    if kind not in SYNC_KINDS:
        raise BoardError(f"kind must be one of {SYNC_KINDS}, got {kind!r}")
    for p in _list(data, "projects"):
        _need(p, "key", "project")
        _need(p, "name", f"project {p.get('key')}")
        for s in _list(p, "statuses"):
            name = _need(s, "name", f"status in {p['key']}")
            if _need(s, "category", f"status {name!r}") not in STATUS_CATEGORIES:
                raise BoardError(f"status {name!r}: category must be one of {STATUS_CATEGORIES}")
    for s in _list(data, "sprints"):
        sid = _need(s, "id", "sprint")
        if _need(s, "state", f"sprint {sid}") not in SPRINT_STATES:
            raise BoardError(f"sprint {sid}: state must be one of {SPRINT_STATES}")
        _need(s, "name", f"sprint {sid}")
        _need(s, "board_id", f"sprint {sid}")
    for t in _list(data, "tickets"):
        key = _need(t, "key", "ticket")
        for field_name in ("project_key", "jira_id", "summary", "status"):
            _need(t, field_name, f"ticket {key}")
        if not isinstance(t.get("labels", []), list):
            raise BoardError(f"ticket {key}: labels must be a list")
    for c in _list(data, "comments"):
        _need(c, "ticket_key", "comment")
        _need(c, "jira_comment_id", "comment")
        _need(c, "author", "comment")
        _need(c, "body", "comment")
        _need(c, "created", "comment")
    return data


# --- sync -----------------------------------------------------------------------

@dataclass
class SyncResult:
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def bump(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [])


def _stage_for(conn: sqlite3.Connection, project_key: str, status: str) -> str | None:
    row = conn.execute(
        "select stage from stage_mapping where project_key = ? and status_name = ?",
        (project_key, status),
    ).fetchone()
    return row["stage"] if row else None


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
                "flags, jira_updated, synced_at, removed_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, null)",
                (key, t["project_key"], str(t["jira_id"]), t["summary"], t.get("description"),
                 t.get("acceptance"), t.get("issue_type"), t.get("priority"), t.get("points"),
                 t.get("assignee_id"), status, t.get("status_category"), _json(labels), sprint_id,
                 stage, t.get("updated"), now),
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
        "sprint_id = ?, stage = ?, jira_updated = ?, synced_at = ?, removed_at = null where key = ?",
        (t["summary"], t.get("description"), t.get("acceptance"), t.get("issue_type"), t.get("priority"),
         t.get("points"), t.get("assignee_id"), status, t.get("status_category"), _json(labels),
         sprint_id, new_stage, t.get("updated"), now, key),
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


# --- reads ----------------------------------------------------------------------

def status_to_stage(conn: sqlite3.Connection) -> dict[str, str]:
    """Jira status name -> internal stage, for routing. Names that map to different
    stages in different projects are left out, because routing can't tell them apart."""
    seen: dict[str, set[str]] = {}
    for r in conn.execute("select status_name, stage from stage_mapping where status_name is not null"):
        seen.setdefault(r["status_name"], set()).add(r["stage"])
    return {name: next(iter(stages)) for name, stages in seen.items() if len(stages) == 1}


def active_board(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from active_sprint_tickets order by stage is null, stage, key"
    ).fetchall()


def backlog(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tickets in no sprint: the signed-in user's backlog, as of the last backlog sync."""
    return conn.execute(
        "select * from backlog_tickets order by stage is null, stage, key"
    ).fetchall()


def unmapped_stages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "select project_key, stage, reason from stage_mapping where status_name is null order by project_key, stage"
    ).fetchall()


def sprints(conn: sqlite3.Connection, state: str | None = None) -> list[sqlite3.Row]:
    if state is None:
        return conn.execute("select * from sprint order by state, start_date, id").fetchall()
    return conn.execute("select * from sprint where state = ? order by start_date, id", (state,)).fetchall()


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


def last_sync(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("select * from sync_run order by id desc limit 1").fetchone()


# --- local changes made by the harness and by agents ----------------------------------

FLAGS = ("needs-human", "blocked")

# Who may move a ticket from one internal stage to another. Derived from the agent
# prompts. The user may move anything. This list is not complete: add a row when a
# prompt gains a new allowed move. Anything not listed is refused.
ALLOWED_MOVES: frozenset[tuple[str, str, str]] = frozenset({
    ("staff", "needs_refinement", "ready_for_dev"),
    ("staff", "in_refinement", "ready_for_dev"),
    ("dev", "ready_for_dev", "in_dev"),
    ("dev", "ready_for_dev", "pr_open"),
    ("dev", "in_dev", "pr_open"),
    ("dev", "pr_in_review", "in_dev"),
    ("dev", "qa_approved", "merged"),
    ("pr_review", "pr_open", "in_dev"),
    ("pr_review", "pr_in_review", "in_dev"),
    ("qa", "ready_for_qa", "qa_approved"),
    ("qa", "ready_for_qa", "in_dev"),
    ("qa", "regression", "in_dev"),
    ("harness", "pr_in_review", "ready_for_qa"),
    ("pr_review", "pr_open", "ready_for_perf_review"),
    ("pr_review", "pr_in_review", "ready_for_perf_review"),
    ("harness", "pr_open", "ready_for_perf_review"),
    ("harness", "pr_in_review", "ready_for_perf_review"),
    ("harness", "ready_for_perf_review", "ready_for_qa"),
    ("perf_review", "ready_for_perf_review", "ready_for_qa"),
    ("perf_review", "ready_for_perf_review", "in_dev"),
    ("qa", "regression", "deployed"),
    ("harness", "merged", "regression"),
    ("harness", "regression", "deployed"),
    ("harness", "performance", "deployed"),
    ("qa", "regression", "performance"),
    ("harness", "regression", "performance"),
    ("harness", "qa_approved", "merged"),
})


def may_move(actor: str, from_stage: str | None, to_stage: str) -> bool:
    if actor == "user":
        return True
    return (actor, from_stage, to_stage) in ALLOWED_MOVES


def move_stage(conn: sqlite3.Connection, key: str, stage: str, actor: str, reason: str,
               clock: Callable[[], float] = time.time) -> None:
    """Move a ticket to an internal stage. Only the stage's mapped status is allowed."""
    if stage not in mapping.STAGES:
        raise BoardError(f"unknown stage {stage!r}; choose one of {', '.join(mapping.STAGES)}")
    row = conn.execute("select * from ticket where key = ?", (key,)).fetchone()
    if row is None:
        raise BoardError(f"no ticket {key}")
    if row["removed_at"] is not None:
        raise BoardError(f"{key} is no longer in scope; it cannot be moved")
    # A stage with no Jira column is internal-only: the move is recorded on the board, and
    # enqueue_transition queues nothing for Jira. Nothing outside the board changes.
    if row["stage"] == stage:
        return
    if not may_move(actor, row["stage"], stage):
        raise BoardError(
            f"{actor!r} may not move {key} from {row['stage'] or '(no stage)'} to {stage!r}"
        )
    with conn:
        conn.execute("update ticket set stage = ? where key = ?", (stage, key))
        _event(conn, key, int(clock()), actor, "stage", row["stage"], stage, reason)
        enqueue_transition(conn, key, stage, actor, int(clock()))


def enqueue_transition(conn: sqlite3.Connection, key: str, stage: str, actor: str, now: int) -> int | None:
    """Queue a move of the real Jira ticket to the status mapped to `stage`.

    Returns None when the stage has no mapped Jira status (nothing to push). A newer
    move supersedes older pending moves for the same ticket.
    """
    row = conn.execute("select project_key from ticket where key = ?", (key,)).fetchone()
    target = conn.execute(
        "select status_name from stage_mapping where project_key = ? and stage = ? and status_name is not null",
        (row["project_key"], stage),
    ).fetchone()
    if target is None:
        return None
    cur = conn.execute(
        "insert into outbox (created_at, ticket_key, kind, target_stage, actor, status) "
        "values (?, ?, 'transition', ?, ?, 'pending')",
        (now, key, stage, actor),
    )
    conn.execute(
        "update outbox set status = 'superseded', updated_at = ? "
        "where ticket_key = ? and kind = 'transition' and status = 'pending' and id != ?",
        (now, key, cur.lastrowid),
    )
    return cur.lastrowid


def set_flag(conn: sqlite3.Connection, key: str, flag: str, actor: str, reason: str,
             clock: Callable[[], float] = time.time) -> None:
    """Flag a ticket for a human. Agents set flags. Only the user clears them."""
    if flag not in FLAGS:
        raise BoardError(f"flag must be one of {FLAGS}")
    row = conn.execute("select flags from ticket where key = ?", (key,)).fetchone()
    if row is None:
        raise BoardError(f"no ticket {key}")
    flags = json.loads(row["flags"])
    if flag in flags:
        return
    flags.append(flag)
    with conn:
        conn.execute("update ticket set flags = ? where key = ?", (json.dumps(flags), key))
        _event(conn, key, int(clock()), actor, "flag", None, flag, reason)


def clear_flag(conn: sqlite3.Connection, key: str, flag: str, actor: str,
               clock: Callable[[], float] = time.time) -> None:
    """Clear a flag. Only the user can do this."""
    if actor != "user":
        raise BoardError("only the user can clear a flag")
    row = conn.execute("select flags from ticket where key = ?", (key,)).fetchone()
    if row is None:
        raise BoardError(f"no ticket {key}")
    flags = json.loads(row["flags"])
    if flag not in flags:
        return
    flags.remove(flag)
    with conn:
        conn.execute("update ticket set flags = ? where key = ?", (json.dumps(flags), key))
        _event(conn, key, int(clock()), actor, "unflag", flag, None, None)


def status(conn: sqlite3.Connection, now: float) -> dict:
    """What the board knows: when it last synced, what is in scope, and what is blocked."""
    run = last_sync(conn)
    return {
        "last_sync": dict(run) if run else None,
        "age_seconds": None if run is None or run["finished_at"] is None else int(now) - run["finished_at"],
        "active_tickets": conn.execute("select count(*) from active_sprint_tickets").fetchone()[0],
        "backlog_tickets": conn.execute("select count(*) from backlog_tickets").fetchone()[0],
        "flagged": [dict(r) for r in conn.execute(
            "select key, flags from ticket where flags != '[]' and removed_at is null order by key")],
        "unmapped_stages": [dict(r) for r in unmapped_stages(conn)],
    }


def add_comment(conn: sqlite3.Connection, key: str, author: str, body: str,
                clock: Callable[[], float] = time.time, origin: str = "agent") -> int:
    """Record an agent's comment on a ticket. It is queued to Jira by the outbox, not sent here."""
    if not body.strip():
        raise BoardError("a comment needs a body")
    now = int(clock())
    with conn:
        if conn.execute("select 1 from ticket where key = ?", (key,)).fetchone() is None:
            raise BoardError(f"no ticket {key}")
        cur = conn.execute(
            "insert into ticket_comment (ticket_key, origin, author, body, created_at) "
            "values (?, ?, ?, ?, ?)", (key, origin, author, body, now))
        _event(conn, key, now, author, "comment", None, None, body[:200])
    return cur.lastrowid


def record_approval(conn: sqlite3.Connection, key: str, role: str, head: str,
                    clock: Callable[[], float] = time.time) -> None:
    """A reviewer approves the ticket at a specific commit. The merge gate reads these."""
    if role not in ("pr_review", "perf_review", "qa", "performance"):
        raise BoardError(f"{role!r} cannot approve a ticket")
    if not head:
        raise BoardError("an approval needs the commit it covers")
    with conn:
        if conn.execute("select 1 from ticket where key = ?", (key,)).fetchone() is None:
            raise BoardError(f"no ticket {key}")
        _event(conn, key, int(clock()), role, "approve", None, head, head)


def approvals_at(conn: sqlite3.Connection, key: str, head: str) -> set[str]:
    """The roles that approved this ticket at exactly this commit."""
    rows = conn.execute(
        "select distinct actor from ticket_event where ticket_key = ? and kind = 'approve' and detail = ?",
        (key, head)).fetchall()
    return {r["actor"] for r in rows}


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


def request_perf_check(conn: sqlite3.Connection, key: str, actor: str, reason: str,
                       clock: Callable[[], float] = time.time) -> None:
    """The dev agent marks a ticket as worth a performance check. Stored on the ticket's history."""
    if actor != "dev":
        raise BoardError("only the dev role can request a performance check")
    if conn.execute("select 1 from ticket where key = ?", (key,)).fetchone() is None:
        raise BoardError(f"no ticket {key}")
    with conn:
        _event(conn, key, int(clock()), actor, "perf_request", None, "requested", reason)


def perf_requested(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute(
        "select 1 from ticket_event where ticket_key = ? and kind = 'perf_request' limit 1", (key,)).fetchone() is not None


def approvals_since_stage(conn: sqlite3.Connection, key: str, head: str, stage: str) -> set[str]:
    """Roles that approved this commit after the ticket last entered `stage`."""
    entered = conn.execute(
        "select max(at) from ticket_event where ticket_key = ? and kind = 'stage' and to_value = ?",
        (key, stage)).fetchone()[0] or 0
    rows = conn.execute(
        "select distinct actor from ticket_event where ticket_key = ? and kind = 'approve' and detail = ? and at >= ?",
        (key, head, entered)).fetchall()
    return {r["actor"] for r in rows}
