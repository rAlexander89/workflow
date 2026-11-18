"""What happens to a ticket: typed domain events, the history record each one leaves, and questions about history.

A Ticket records an event for every change it accepts. The repository writes `event.record()` to the ticket's
history, and the application publishes the event so other parts of the system can react to it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TicketEvent:
    """One line of a ticket's history, as stored."""
    ticket_key: str
    at: int
    actor: str
    kind: str  # stage, flag, unflag, comment, approve, perf_request, link, order, outbox, sync, ...
    from_value: str | None = None
    to_value: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DomainEvent:
    """Something that happened to a ticket. Subclasses say what, and how it reads in the history."""
    ticket_key: str
    at: int
    actor: str

    def record(self) -> TicketEvent:
        raise NotImplementedError


@dataclass(frozen=True)
class TicketMoved(DomainEvent):
    project_key: str
    from_stage: str | None
    to_stage: str
    reason: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "stage",
                           None if self.from_stage is None else str(self.from_stage), str(self.to_stage), self.reason)


@dataclass(frozen=True)
class TicketFlagged(DomainEvent):
    flag: str
    reason: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "flag", None, str(self.flag), self.reason)


@dataclass(frozen=True)
class FlagCleared(DomainEvent):
    flag: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "unflag", str(self.flag), None, None)


@dataclass(frozen=True)
class TicketCommented(DomainEvent):
    preview: str  # the first 200 characters

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "comment", None, None, self.preview)


@dataclass(frozen=True)
class ApprovalRecorded(DomainEvent):
    """`actor` is the approving role. The approval covers exactly this commit."""
    commit: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "approve", None, self.commit, self.commit)


@dataclass(frozen=True)
class PerfCheckRequested(DomainEvent):
    reason: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "perf_request", None, "requested", self.reason)


@dataclass(frozen=True)
class TicketReordered(DomainEvent):
    previous: int | None
    position: int

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "order",
                           None if self.previous is None else str(self.previous), str(self.position), "work order")


@dataclass(frozen=True)
class TicketBlocked(DomainEvent):
    """This ticket must wait for `prerequisite` to merge to dev."""
    prerequisite: str
    reason: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "link", None, self.prerequisite, self.reason)


@dataclass(frozen=True)
class SentToJira(DomainEvent):
    jira_status: str

    def record(self) -> TicketEvent:
        return TicketEvent(self.ticket_key, self.at, self.actor, "outbox", None, self.jira_status, "sent to Jira")


# --- questions about a ticket's history --------------------------------------------------------

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
