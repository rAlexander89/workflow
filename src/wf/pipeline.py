"""Pipeline: the one path from a Jira trigger to the supervisor.

A webhook gives one issue key. The hourly poll gives a JQL search. Both re-read the
issue's changelog, record new status changes once, and send each new change through
the supervisor.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from wf.events import ChangeStore, Intake, poll_due
from wf.orchestrator import Decision, Dispatch, LifecycleStore, Supervisor, no_agents_wired


class Pipeline:
    def __init__(
        self,
        db_path: Path,
        fetch_changelog: Callable[[str], dict],
        search_keys: Callable[[], list[str]],
        dispatch: Dispatch = no_agents_wired,
        clock: Callable[[], float] = time.time,
    ):
        self.changes = ChangeStore(Path(db_path), clock=clock)
        self.intake = Intake(self.changes, fetch_changelog)
        self.lifecycle = LifecycleStore(Path(db_path), clock=clock)
        self.supervisor = Supervisor(self.lifecycle, dispatch)
        self._search = search_keys
        self._clock = clock

    def _route(self, issue_key: str) -> list[Decision]:
        changes, labels = self.intake.fetch(issue_key)
        return [self.supervisor.handle(c, labels) for c in changes]

    def on_webhook(self, issue_key: str) -> list[Decision]:
        self.changes.mark_webhook()
        return self._route(issue_key)

    def poll(self, now: float | None = None) -> list[Decision]:
        """Hourly fallback. Does nothing while webhooks are arriving."""
        now = self._clock() if now is None else now
        if not poll_due(self.changes.last_webhook_at(), now):
            return []
        decisions: list[Decision] = []
        for key in self._search():
            decisions.extend(self._route(key))
        return decisions
