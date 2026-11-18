"""Supervisor: routes each ticket status change to the agent that owns its stage.

A Jira status is mapped to an internal stage (see `wf.mapping`), and the stage has an
owner. Both steps are fixed rules, not a model call. A status with no stage is
"unknown" and nothing is dispatched. Every decision is written to the
local lifecycle record. A kill switch halts all dispatch, and a loop guard
escalates a ticket that keeps re-entering the same status.

Agents are not built yet. The dispatch callable is injected, so until agents
exist the supervisor records the handoff and does nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from wf.adapters.persistence.sqlite.lifecycle import SCHEMA, SqliteLifecycleLog as LifecycleStore  # noqa: F401
from wf.application.tracker.supervisor import Dispatch, Supervisor as _Supervisor, no_agents_wired  # noqa: F401
from wf.domain.delivery.routing import (  # noqa: F401
    HOTFIX_LABEL, MAX_DISPATCHES_PER_STATUS, STAGE_OWNERS, Decision, route,
)
from wf.domain.tracker.changelog import StatusChange  # noqa: F401


def board_status_to_stage(path: Path) -> dict[str, str]:
    """The stage mapping from the board's SQLite file. Empty until a sync has run."""
    from wf import board

    conn = board.connect(Path(path))
    try:
        return board.status_to_stage(conn)
    finally:
        conn.close()


class Supervisor(_Supervisor):
    """The supervisor over a lifecycle store; the stage mapping defaults to the store's own database."""

    def __init__(
        self,
        store: LifecycleStore,
        dispatch: Dispatch = no_agents_wired,
        max_dispatches_per_status: int = MAX_DISPATCHES_PER_STATUS,
        status_to_stage: Callable[[], Mapping[str, str]] | None = None,
    ):
        super().__init__(store, status_to_stage or (lambda: board_status_to_stage(store.path)), dispatch,
                         max_dispatches_per_status)
