"""The outbox: moves waiting to be pushed to Jira, and the rules for deciding what to do with each."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    FAILED = "failed"
    SUPERSEDED = "superseded"


# An item in one of these states can be put back to pending by a person.
RETRYABLE = (OutboxStatus.BLOCKED, OutboxStatus.CONFLICT, OutboxStatus.FAILED, OutboxStatus.SENDING)


def can_retry(status: str) -> bool:
    return status in RETRYABLE


@dataclass(frozen=True)
class Observation:
    """What the Jira session saw for a ticket: its current status and the transitions it could take."""
    status: str
    transitions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class OutboxDecision:
    status: OutboxStatus
    detail: str | None = None
    transition_id: str | None = None  # set when the status is SENDING
    flag_reason: str | None = None  # set when a person has to look
    synced_jira_status: str | None = None  # Jira already shows the target: remember it


def decide(*, target_status: str | None, observation: Observation | None, last_seen_status: str) -> OutboxDecision | None:
    """What to do with one pending move, or None when the Jira session has not looked at the ticket yet.

    - blocked  : the stage has no mapped Jira status, or Jira has no transition to it from where it is
    - skipped  : Jira is already in the target status
    - conflict : Jira's status changed since our last sync. Not forced. The ticket is flagged for a person.
    - sending  : Jira allows the move. It is marked so it is never sent twice.
    """
    if target_status is None:
        return OutboxDecision(OutboxStatus.BLOCKED, "the stage has no mapped Jira status")
    if observation is None:
        return None
    current = observation.status
    if current == target_status:
        return OutboxDecision(OutboxStatus.SKIPPED, f"already in {target_status!r}", synced_jira_status=current)
    if current != last_seen_status:
        return OutboxDecision(
            OutboxStatus.CONFLICT, f"Jira status is {current!r}, last seen {last_seen_status!r}; not forced",
            flag_reason=f"Jira changed to {current!r} since last sync; a move to {target_status!r} is waiting")
    transition = next((t for t in observation.transitions if t.get("to") == target_status), None)
    if transition is None:
        return OutboxDecision(OutboxStatus.BLOCKED, f"Jira has no transition from {current!r} to {target_status!r}")
    return OutboxDecision(OutboxStatus.SENDING, transition_id=transition["id"])
