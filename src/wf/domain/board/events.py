"""A ticket's history: what happened to it, and the questions the pipeline asks of that history."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TicketEvent:
    ticket_key: str
    at: int
    actor: str
    kind: str  # stage, flag, unflag, comment, approve, perf_request, link, order, sync, ...
    from_value: str | None = None
    to_value: str | None = None
    detail: str | None = None


def approvals_at(history: list[TicketEvent], head: str) -> set[str]:
    """The roles that approved the ticket at exactly this commit."""
    return {e.actor for e in history if e.kind == "approve" and e.detail == head}


def approvals_since_stage(history: list[TicketEvent], head: str, stage: str) -> set[str]:
    """Roles that approved this commit after the ticket last entered `stage`."""
    entered = max((e.at for e in history if e.kind == "stage" and e.to_value == stage), default=0)
    return {e.actor for e in history if e.kind == "approve" and e.detail == head and e.at >= entered}


def passed_regression(history: list[TicketEvent]) -> bool:
    """QA moved the ticket from regression to deployed: it passed regression on dev."""
    return any(e.kind == "stage" and e.from_value == "regression" and e.to_value == "deployed"
               and e.actor == "qa" for e in history)


def perf_requested(history: list[TicketEvent]) -> bool:
    return any(e.kind == "perf_request" for e in history)


def changed_since(history: list[TicketEvent], at: int, by_anyone_but: str) -> bool:
    """True if anyone other than `by_anyone_but` did something to the ticket at or after `at`."""
    return any(e.at >= at and e.actor != by_anyone_but for e in history)
