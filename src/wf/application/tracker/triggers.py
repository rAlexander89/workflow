"""Triggers: the one path from a Jira webhook or poll to the supervisor."""

from __future__ import annotations

from typing import Callable

from wf.application.ports.persistence import ChangeLog
from wf.application.tracker.intake import Intake
from wf.application.tracker.supervisor import Supervisor
from wf.domain.delivery.routing import Decision
from wf.domain.tracker.changelog import poll_due


class TriggerPipeline:
    """A webhook gives one issue key. The hourly poll gives a JQL search. Both re-read the issue's
    changelog, record new status changes once, and send each new change through the supervisor."""

    def __init__(self, *, changes: ChangeLog, intake: Intake, supervisor: Supervisor,
                 search_keys: Callable[[], list[str]], clock: Callable[[], float]):
        self.changes = changes
        self.intake = intake
        self.supervisor = supervisor
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
