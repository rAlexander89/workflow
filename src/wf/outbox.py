"""The outbox: board changes waiting to be pushed to Jira by the Jira agent.

The Atlassian MCP lives in the user's Claude session, so the Jira agent runs there in
three steps. The Python side decides; the session only calls Jira.

1. `pending()` lists the tickets that have queued moves.
2. The session reads each ticket's current Jira status and allowed transitions through
   the MCP, and writes them to a JSON file of observations.
3. `plan(observations)` decides each queued move:
     - send     : Jira allows the move. The item is marked `sending`, so it is never
                  sent twice. The session calls the transition through the MCP.
     - skipped  : Jira is already in the target status.
     - conflict : Jira's status changed since our last sync. Not forced. The ticket is
                  flagged for the user.
     - blocked  : Jira has no transition to the target from its current status.
4. The session reports each result with `ack(results)`. A sent item is final.
   A failed or stuck item can be put back with `retry(id)`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable

from wf import board

RETRYABLE = ("blocked", "conflict", "failed", "sending")


def pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "select o.id, o.ticket_key, o.target_stage, t.project_key, t.jira_status "
        "from outbox o join ticket t on t.key = o.ticket_key "
        "where o.kind = 'transition' and o.status = 'pending' order by o.id"
    ).fetchall()
    return [dict(r) for r in rows]


def _set(conn, row_id: int, status: str, now: int, detail: str | None = None, attempts_bump: bool = False) -> None:
    conn.execute(
        "update outbox set status = ?, detail = ?, updated_at = ?, attempts = attempts + ? where id = ?",
        (status, detail, now, 1 if attempts_bump else 0, row_id),
    )


def plan(conn: sqlite3.Connection, observations: dict[str, Any],
         clock: Callable[[], float] = time.time) -> dict:
    """Decide what to send, given the Jira state the session observed.

    `observations` maps issue key -> {"status": <current Jira status name>,
    "transitions": [{"id": <transition id>, "to": <status name it leads to>}, ...]}.
    """
    now = int(clock())
    result: dict[str, Any] = {"need": [], "send": [], "decided": {}}

    def count(status: str) -> None:
        result["decided"][status] = result["decided"].get(status, 0) + 1

    for item in pending(conn):
        key = item["ticket_key"]
        target = conn.execute(
            "select status_name from stage_mapping where project_key = ? and stage = ? and status_name is not null",
            (item["project_key"], item["target_stage"]),
        ).fetchone()
        if target is None:
            _set(conn, item["id"], "blocked", now, "the stage has no mapped Jira status")
            count("blocked")
            continue
        target_status = target["status_name"]

        obs = observations.get(key)
        if obs is None:
            result["need"].append(key)
            continue
        current = obs["status"]

        if current == target_status:
            _set(conn, item["id"], "skipped", now, f"already in {target_status!r}")
            conn.execute("update ticket set jira_status = ? where key = ?", (current, key))
            count("skipped")
        elif current != item["jira_status"]:
            _set(conn, item["id"], "conflict", now,
                 f"Jira status is {current!r}, last seen {item['jira_status']!r}; not forced")
            board.set_flag(conn, key, "needs-human", "outbox",
                           f"Jira changed to {current!r} since last sync; a move to {target_status!r} is waiting",
                           clock=clock)
            count("conflict")
        else:
            transition = next((t for t in obs.get("transitions", []) if t.get("to") == target_status), None)
            if transition is None:
                _set(conn, item["id"], "blocked", now,
                     f"Jira has no transition from {current!r} to {target_status!r}")
                count("blocked")
            else:
                _set(conn, item["id"], "sending", now, attempts_bump=True)
                result["send"].append({"id": item["id"], "key": key,
                                       "transition_id": transition["id"], "to_status": target_status})
                count("sending")
        conn.commit()
    conn.commit()
    return result


def ack(conn: sqlite3.Connection, results: list[dict],
        clock: Callable[[], float] = time.time) -> dict:
    """Record what the session got back from Jira for each sent item."""
    now = int(clock())
    counts = {"sent": 0, "failed": 0, "ignored": 0}
    for r in results:
        row = conn.execute("select * from outbox where id = ?", (r["id"],)).fetchone()
        if row is None or row["status"] != "sending":
            counts["ignored"] += 1
            continue
        if r.get("ok"):
            status_after = r.get("jira_status") or row["target_stage"]
            conn.execute(
                "update outbox set status = 'sent', jira_response = ?, updated_at = ? where id = ?",
                (json.dumps(r.get("response")), now, r["id"]),
            )
            conn.execute("update ticket set jira_status = ? where key = ?", (status_after, row["ticket_key"]))
            board._event(conn, row["ticket_key"], now, "jira-agent", "outbox", None, status_after, "sent to Jira")
            counts["sent"] += 1
        else:
            _set(conn, r["id"], "failed", now, str(r.get("error", "unknown error"))[:500])
            counts["failed"] += 1
    conn.commit()
    return counts


def retry(conn: sqlite3.Connection, row_id: int, clock: Callable[[], float] = time.time) -> None:
    row = conn.execute("select status from outbox where id = ?", (row_id,)).fetchone()
    if row is None:
        raise board.BoardError(f"no outbox item {row_id}")
    if row["status"] not in RETRYABLE:
        raise board.BoardError(f"item {row_id} is {row['status']!r}; only {', '.join(RETRYABLE)} can be retried")
    _set(conn, row_id, "pending", int(clock()), "retry requested")
    conn.commit()


def listing(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status is None:
        rows = conn.execute("select * from outbox order by id").fetchall()
    else:
        rows = conn.execute("select * from outbox where status = ? order by id", (status,)).fetchall()
    return [dict(r) for r in rows]
