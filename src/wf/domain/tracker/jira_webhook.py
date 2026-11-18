"""Jira webhooks: a ticket changed in Jira, so read Jira again now.

Jira Cloud has no per-delivery signature we can rely on for every event type, so the
receiver URL carries a secret token (WF_JIRA_WEBHOOK_TOKEN). A request with a wrong token is
refused. A delivery only says "look now": the sync reads the tickets again through the
Atlassian connection, and that read is the state the board uses.
"""

from __future__ import annotations

import hmac

RELEVANT_EVENTS = {
    "jira:issue_created", "jira:issue_updated", "jira:issue_deleted",
    "sprint_created", "sprint_started", "sprint_updated", "sprint_closed",
}


def token_ok(expected: str, given: str | None) -> bool:
    if not expected or not given:
        return False
    return hmac.compare_digest(expected, given)


def is_relevant(event: str | None) -> bool:
    return event in RELEVANT_EVENTS
