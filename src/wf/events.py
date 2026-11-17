"""Jira status-change intake.

Webhooks and the hourly poll are only triggers. On either one, the issue is
re-read from the Jira REST API with its changelog, and each status change in it
is recorded once, keyed by (issue key, history id). The webhook payload's own
changelog is never trusted, since Jira can send it missing or stale.
"""

from __future__ import annotations

import time
from typing import Callable

from wf.adapters.persistence.sqlite.changes import SCHEMA, SqliteChangeLog as ChangeStore  # noqa: F401
from wf.domain.tracker.changelog import (  # noqa: F401
    POLL_AFTER_SILENCE_SECONDS, StatusChange, issue_key_from_webhook, labels_from_body, parse_changelog, poll_due,
)


class Intake:
    """Turns a trigger (one issue key) into the status changes not yet recorded."""

    def __init__(self, store: ChangeStore, fetch_changelog: Callable[[str], dict]):
        self.store = store
        self._fetch = fetch_changelog

    def fetch(self, issue_key: str) -> tuple[list[StatusChange], set[str]]:
        """New status changes for this issue, plus its current labels."""
        body = self._fetch(issue_key)
        new = self.store.record_new(parse_changelog(issue_key, body))
        return new, labels_from_body(body)

    def handle(self, issue_key: str) -> list[StatusChange]:
        return self.fetch(issue_key)[0]

    def poll(self, search_keys: Callable[[], list[str]], now: float | None = None) -> list[StatusChange]:
        """Hourly fallback. Does nothing if a webhook arrived recently."""
        now = self.store._clock() if now is None else now
        if not poll_due(self.store.last_webhook_at(), now):
            return []
        found: list[StatusChange] = []
        for key in search_keys():
            found.extend(self.handle(key))
        return found
