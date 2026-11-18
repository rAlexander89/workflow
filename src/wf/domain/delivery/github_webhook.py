"""GitHub webhooks: verify that a delivery really came from GitHub, and decide whether it matters.

GitHub signs each delivery with the webhook's secret (HMAC-SHA256, sent as X-Hub-Signature-256).
A delivery with a missing or wrong signature is refused. With no secret configured, every
delivery is refused.

The events that matter are the ones that change a ticket's state: a CI run finishing, a PR
changing, or a check suite finishing. Each one wakes the auto loop, which then reads the forge
as it always does. The webhook only says "look now"; the state still comes from the forge.
"""

from __future__ import annotations

import hashlib
import hmac

RELEVANT_EVENTS = {"workflow_run", "pull_request", "check_suite", "check_run", "deployment_status"}


def signature_ok(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def is_relevant(event: str) -> bool:
    return event in RELEVANT_EVENTS
