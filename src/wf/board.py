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

import sqlite3
import time
from typing import Callable

from wf import mapping
from wf.adapters.persistence.sqlite.board import SqliteBoardUnitOfWork
from wf.adapters.persistence.sqlite.board_queries import (  # noqa: F401
    active_board, backlog, counts, last_sync, sprints, status_to_stage, ticket_detail, unmapped_stages,
)
from wf.adapters.persistence.sqlite.board_sync import (  # noqa: F401
    _stage_for, apply_payload, carry_workflow_statuses, sync_file,
)
from wf.adapters.persistence.sqlite.schema import LINK_SCHEMA, SCHEMA, SCHEMA_VERSION, connect  # noqa: F401
from wf.application.board.tickets import TicketCommands
from wf.domain.board.errors import BoardError  # noqa: F401
from wf.domain.board.moves import ALLOWED_MOVES, FLAGS, may_move  # noqa: F401
from wf.domain.board.payload import (  # noqa: F401
    SPRINT_STATES, STATUS_CATEGORIES, SYNC_KINDS, SyncResult, validate,
)


def _commands(conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> TicketCommands:
    """Ticket commands over this connection. The legacy functions below call them."""
    return TicketCommands(lambda: SqliteBoardUnitOfWork(conn), clock)


# --- local changes made by the harness and by agents ----------------------------------

def move_stage(conn: sqlite3.Connection, key: str, stage: str, actor: str, reason: str,
               clock: Callable[[], float] = time.time) -> None:
    """Move a ticket to an internal stage. Only the stage's mapped status is allowed."""
    _commands(conn, clock).move_stage(key, stage, actor, reason)


def enqueue_transition(conn: sqlite3.Connection, key: str, stage: str, actor: str, now: int) -> int | None:
    """Queue a move of the real Jira ticket to the status mapped to `stage`.

    Returns None when the stage has no mapped Jira status (nothing to push). A newer
    move supersedes older pending moves for the same ticket.
    """
    return _commands(conn, lambda: now).queue_transition(key, stage, actor)


def set_flag(conn: sqlite3.Connection, key: str, flag: str, actor: str, reason: str,
             clock: Callable[[], float] = time.time) -> None:
    """Flag a ticket for a human. Agents set flags. Only the user clears them."""
    _commands(conn, clock).set_flag(key, flag, actor, reason)


def clear_flag(conn: sqlite3.Connection, key: str, flag: str, actor: str,
               clock: Callable[[], float] = time.time) -> None:
    """Clear a flag. Only the user can do this."""
    _commands(conn, clock).clear_flag(key, flag, actor)


def status(conn: sqlite3.Connection, now: float) -> dict:
    """What the board knows: when it last synced, what is in scope, and what is blocked."""
    run = last_sync(conn)
    return {
        "last_sync": run,
        "age_seconds": None if run is None or run["finished_at"] is None else int(now) - run["finished_at"],
        **counts(conn),
    }


def add_comment(conn: sqlite3.Connection, key: str, author: str, body: str,
                clock: Callable[[], float] = time.time, origin: str = "agent") -> int:
    """Record an agent's comment on a ticket. It is queued to Jira by the outbox, not sent here."""
    return _commands(conn, clock).add_comment(key, author, body, origin)


def record_approval(conn: sqlite3.Connection, key: str, role: str, head: str,
                    clock: Callable[[], float] = time.time) -> None:
    """A reviewer approves the ticket at a specific commit. The merge gate reads these."""
    _commands(conn, clock).record_approval(key, role, head)


def approvals_at(conn: sqlite3.Connection, key: str, head: str) -> set[str]:
    """The roles that approved this ticket at exactly this commit."""
    return _commands(conn).approvals_at(key, head)


def request_perf_check(conn: sqlite3.Connection, key: str, actor: str, reason: str,
                       clock: Callable[[], float] = time.time) -> None:
    """The dev agent marks a ticket as worth a performance check. Stored on the ticket's history."""
    _commands(conn, clock).request_perf_check(key, actor, reason)


def changed_since(conn: sqlite3.Connection, key: str, at: int, by_anyone_but: str) -> bool:
    """True if anyone but this actor did something to the ticket at or after `at`."""
    return _commands(conn).changed_since(key, at, by_anyone_but)


def perf_requested(conn: sqlite3.Connection, key: str) -> bool:
    return _commands(conn).perf_requested(key)


def approvals_since_stage(conn: sqlite3.Connection, key: str, head: str, stage: str) -> set[str]:
    """Roles that approved this commit after the ticket last entered `stage`."""
    return _commands(conn).approvals_since_stage(key, head, stage)


# --- ordering and dependencies --------------------------------------------------

def add_block(conn: sqlite3.Connection, prerequisite: str, dependent: str, actor: str, reason: str,
              clock: Callable[[], float] = time.time) -> None:
    """Record that `prerequisite` must merge before `dependent` starts. Jira gets the link on the next send."""
    _commands(conn, clock).add_block(prerequisite, dependent, actor, reason)


def blockers(conn: sqlite3.Connection, key: str) -> list[str]:
    return _commands(conn).blockers(key)


def pending_links(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(prerequisite, dependent) pairs that are not yet in Jira."""
    return _commands(conn).pending_links()


def mark_linked(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> None:
    _commands(conn).mark_linked(pairs)


def set_order(conn: sqlite3.Connection, key: str, position: int, actor: str,
              clock: Callable[[], float] = time.time) -> None:
    """Where the ticket sits in the sprint's work order. Lower goes first. Unordered tickets go last."""
    _commands(conn, clock).set_order(key, position, actor)
