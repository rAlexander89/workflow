"""The outbox: board changes waiting to be pushed to Jira by the Jira agent. The rules are in
`wf.application.tracker.outbox`; these functions keep the old connection-based API.

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

import sqlite3
import time
from typing import Any, Callable

from wf.adapters.persistence.sqlite.board import SqliteBoardUnitOfWork
from wf.application.tracker.outbox import RETRYABLE, OutboxService  # noqa: F401


def _service(conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> OutboxService:
    return OutboxService(lambda: SqliteBoardUnitOfWork(conn), clock)


def pending(conn: sqlite3.Connection) -> list[dict]:
    return _service(conn).pending()


def plan(conn: sqlite3.Connection, observations: dict[str, Any], clock: Callable[[], float] = time.time) -> dict:
    """Decide what to send, given the Jira state the session observed."""
    return _service(conn, clock).plan(observations)


def ack(conn: sqlite3.Connection, results: list[dict], clock: Callable[[], float] = time.time) -> dict:
    """Record what the session got back from Jira for each sent item."""
    return _service(conn, clock).ack(results)


def retry(conn: sqlite3.Connection, row_id: int, clock: Callable[[], float] = time.time) -> None:
    _service(conn, clock).retry(row_id)


def listing(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    return _service(conn).listing(status)


def for_ticket(conn: sqlite3.Connection, key: str) -> list[dict]:
    """The ticket's queued moves, oldest first."""
    return _service(conn).for_ticket(key)


def pending_count(conn: sqlite3.Connection) -> int:
    return _service(conn).pending_count()
