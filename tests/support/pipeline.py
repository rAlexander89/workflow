"""Pipeline: the one path from a Jira trigger to the supervisor.

A webhook gives one issue key. The hourly poll gives a JQL search. Both re-read the
issue's changelog, record new status changes once, and send each new change through
the supervisor.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping

from wf.application.tracker.triggers import TriggerPipeline
from tests.support.events import ChangeStore, Intake, poll_due  # noqa: F401
from tests.support.orchestrator import Decision, Dispatch, LifecycleStore, Supervisor, no_agents_wired  # noqa: F401


class Pipeline(TriggerPipeline):
    """The trigger pipeline over the SQLite stores in one database file."""

    def __init__(
        self,
        db_path: Path,
        fetch_changelog: Callable[[str], dict],
        search_keys: Callable[[], list[str]],
        dispatch: Dispatch = no_agents_wired,
        clock: Callable[[], float] = time.time,
        status_to_stage: Callable[[], Mapping[str, str]] | None = None,
    ):
        changes = ChangeStore(Path(db_path), clock=clock)
        lifecycle = LifecycleStore(Path(db_path), clock=clock)
        super().__init__(
            changes=changes, intake=Intake(changes, fetch_changelog),
            supervisor=Supervisor(lifecycle, dispatch, status_to_stage=status_to_stage),
            search_keys=search_keys, clock=clock)
        self.lifecycle = lifecycle
