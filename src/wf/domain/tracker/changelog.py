"""Jira status changes: parsing an issue's changelog, and when the hourly poll is due."""

from __future__ import annotations

from dataclasses import dataclass

POLL_AFTER_SILENCE_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class StatusChange:
    issue_key: str
    history_id: str
    from_status: str
    to_status: str
    created: str


def parse_changelog(issue_key: str, body: dict) -> list[StatusChange]:
    """Status transitions from an issue body fetched with expand=changelog."""
    out: list[StatusChange] = []
    for history in body.get("changelog", {}).get("histories", []):
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            out.append(
                StatusChange(
                    issue_key=issue_key,
                    history_id=str(history["id"]),
                    from_status=item.get("fromString") or "",
                    to_status=item.get("toString") or "",
                    created=history.get("created", ""),
                )
            )
    return out


def labels_from_body(body: dict) -> set[str]:
    return set((body.get("fields") or {}).get("labels") or [])


def issue_key_from_webhook(payload: dict) -> str | None:
    key = (payload.get("issue") or {}).get("key")
    return key if isinstance(key, str) and key else None


def poll_due(last_webhook_at: float | None, now: float) -> bool:
    """The hourly poll runs only when no webhook has arrived in the last two hours."""
    return last_webhook_at is None or now - last_webhook_at >= POLL_AFTER_SILENCE_SECONDS
