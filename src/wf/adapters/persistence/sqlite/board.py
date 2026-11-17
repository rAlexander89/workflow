"""SQLite implementations of the board ports, over the board's connection (rows are sqlite3.Row)."""

from __future__ import annotations

import json
import sqlite3

from wf.domain.board.events import TicketEvent
from wf.domain.board.ticket import Ticket


class SqliteTicketRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, key: str) -> Ticket | None:
        row = self._conn.execute(
            "select key, project_key, stage, flags, removed_at, sort_order, jira_status, summary "
            "from ticket where key = ?",
            (key,)).fetchone()
        if row is None:
            return None
        return Ticket(row["key"], row["project_key"], row["stage"], json.loads(row["flags"] or "[]"),
                      row["removed_at"] is not None, row["sort_order"], row["jira_status"], row["summary"])

    def save(self, ticket: Ticket) -> None:
        self._conn.execute("update ticket set stage = ?, flags = ?, sort_order = ? where key = ?",
                           (ticket.stage, json.dumps(ticket.flags), ticket.sort_order, ticket.key))
        for e in ticket.events:
            self._conn.execute(
                "insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (e.ticket_key, e.at, e.actor, e.kind, e.from_value, e.to_value, e.detail))
        ticket.events.clear()

    def history(self, key: str) -> list[TicketEvent]:
        rows = self._conn.execute(
            "select ticket_key, at, actor, kind, from_value, to_value, detail from ticket_event "
            "where ticket_key = ? order by at, id", (key,)).fetchall()
        return [TicketEvent(**dict(r)) for r in rows]

    def add_comment(self, key: str, origin: str, author: str, body: str, at: int) -> int:
        cur = self._conn.execute(
            "insert into ticket_comment (ticket_key, origin, author, body, created_at) values (?, ?, ?, ?, ?)",
            (key, origin, author, body, at))
        return cur.lastrowid

    def last_event_detail(self, key: str, kind: str) -> str | None:
        row = self._conn.execute(
            "select detail from ticket_event where ticket_key = ? and kind = ? order by id desc limit 1",
            (key, kind)).fetchone()
        return None if row is None else row[0]

    def event_details(self, key: str, kind: str) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "select detail from ticket_event where ticket_key = ? and kind = ? order by id desc", (key, kind))]

    def append_event(self, event: TicketEvent) -> None:
        self._conn.execute(
            "insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (event.ticket_key, event.at, event.actor, event.kind, event.from_value, event.to_value, event.detail))

    def link_blocks(self, prerequisite: str, dependent: str) -> None:
        self._conn.execute(
            "insert or ignore into ticket_link (ticket_key, other_key, link_type, direction, synced) "
            "values (?, ?, 'Blocks', 'outward', 0)", (prerequisite, dependent))
        self._conn.execute(
            "insert or ignore into ticket_link (ticket_key, other_key, link_type, direction, synced) "
            "values (?, ?, 'is blocked by', 'inward', 0)", (dependent, prerequisite))

    def set_jira_status(self, key: str, status: str) -> None:
        self._conn.execute("update ticket set jira_status = ? where key = ?", (status, key))

    def blockers(self, key: str) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "select other_key from ticket_link where ticket_key = ? and link_type = 'is blocked by' "
            "order by other_key", (key,))]

    def pending_links(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self._conn.execute(
            "select ticket_key, other_key from ticket_link where link_type = 'Blocks' and direction = 'outward' "
            "and synced = 0 order by ticket_key, other_key")]

    def mark_linked(self, pairs: list[tuple[str, str]]) -> None:
        for prerequisite, dependent in pairs:
            self._conn.execute(
                "update ticket_link set synced = 1 where ticket_key = ? and other_key = ? and link_type = 'Blocks'",
                (prerequisite, dependent))
            self._conn.execute(
                "update ticket_link set synced = 1 where ticket_key = ? and other_key = ? "
                "and link_type = 'is blocked by'", (dependent, prerequisite))


class SqliteStageMappings:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def status_for(self, project_key: str, stage: str) -> str | None:
        row = self._conn.execute(
            "select status_name from stage_mapping where project_key = ? and stage = ? and status_name is not null",
            (project_key, stage)).fetchone()
        return None if row is None else row["status_name"]


class SqliteOutboxQueue:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def enqueue_transition(self, ticket_key: str, stage: str, actor: str, now: int) -> int:
        cur = self._conn.execute(
            "insert into outbox (created_at, ticket_key, kind, target_stage, actor, status) "
            "values (?, ?, 'transition', ?, ?, 'pending')", (now, ticket_key, stage, actor))
        self._conn.execute(
            "update outbox set status = 'superseded', updated_at = ? "
            "where ticket_key = ? and kind = 'transition' and status = 'pending' and id != ?",
            (now, ticket_key, cur.lastrowid))
        return cur.lastrowid

    def pending(self) -> list[dict]:
        rows = self._conn.execute(
            "select o.id, o.ticket_key, o.target_stage, t.project_key, t.jira_status "
            "from outbox o join ticket t on t.key = o.ticket_key "
            "where o.kind = 'transition' and o.status = 'pending' order by o.id").fetchall()
        return [dict(r) for r in rows]

    def get(self, item_id: int) -> dict | None:
        row = self._conn.execute("select * from outbox where id = ?", (item_id,)).fetchone()
        return None if row is None else dict(row)

    def update(self, item_id: int, status: str, now: int, detail: str | None = None,
               attempts_bump: bool = False) -> None:
        self._conn.execute(
            "update outbox set status = ?, detail = ?, updated_at = ?, attempts = attempts + ? where id = ?",
            (status, detail, now, 1 if attempts_bump else 0, item_id))

    def mark_sent(self, item_id: int, now: int, response_json: str) -> None:
        self._conn.execute(
            "update outbox set status = 'sent', jira_response = ?, updated_at = ? where id = ?",
            (response_json, now, item_id))

    def listing(self, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self._conn.execute("select * from outbox order by id").fetchall()
        else:
            rows = self._conn.execute("select * from outbox where status = ? order by id", (status,)).fetchall()
        return [dict(r) for r in rows]

    def for_ticket(self, key: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "select * from outbox where ticket_key = ? and kind = 'transition' order by id", (key,)).fetchall()]

    def pending_count(self) -> int:
        return self._conn.execute(
            "select count(*) from outbox where kind = 'transition' and status = 'pending'").fetchone()[0]


class SqliteBoardUnitOfWork:
    """A transaction on an open board connection. It does not own the connection or close it."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.tickets = SqliteTicketRepository(conn)
        self.mappings = SqliteStageMappings(conn)
        self.outbox = SqliteOutboxQueue(conn)

    def __enter__(self) -> "SqliteBoardUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
