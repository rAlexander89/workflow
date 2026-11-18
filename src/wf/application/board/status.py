"""What the board knows: when it last synced, what is in scope, and what is blocked."""

from __future__ import annotations

from wf.application.ports.board import BoardQueries


def board_status(queries: BoardQueries, now: float, kind: str | None = None) -> dict:
    """The newest sync (of this kind, if given) and how old it is, plus the ticket counts."""
    last = queries.last_sync(kind)
    age = None if last is None or last["finished_at"] is None else int(now) - last["finished_at"]
    return {"last_sync": last, "age_seconds": age, **queries.counts()}
