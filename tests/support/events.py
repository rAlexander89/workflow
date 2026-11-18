"""Jira status-change intake.

Webhooks and the hourly poll are only triggers. On either one, the issue is
re-read from the Jira REST API with its changelog, and each status change in it
is recorded once, keyed by (issue key, history id). The webhook payload's own
changelog is never trusted, since Jira can send it missing or stale.
"""

from __future__ import annotations

from typing import Callable

from wf.adapters.persistence.sqlite.changes import SCHEMA, SqliteChangeLog as ChangeStore  # noqa: F401
from wf.application.tracker.intake import Intake as _Intake
from wf.domain.tracker.changelog import (  # noqa: F401
    POLL_AFTER_SILENCE_SECONDS, StatusChange, issue_key_from_webhook, labels_from_body, parse_changelog, poll_due,
)


class Intake(_Intake):
    """Intake over a change store; its clock is the store's clock."""

    def __init__(self, store: ChangeStore, fetch_changelog: Callable[[str], dict]):
        super().__init__(store, fetch_changelog, store._clock)
