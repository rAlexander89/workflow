"""Board ports: the ticket repository, the Jira outbox queue, the stage mapping, and the unit of work."""

from __future__ import annotations

from typing import Protocol

from wf.domain.board.events import TicketEvent
from wf.domain.board.ticket import Ticket


class TicketRepository(Protocol):
    def get(self, key: str) -> Ticket | None:
        """The ticket, including one removed from scope (see `Ticket.removed`), or None if unknown."""
        ...

    def save(self, ticket: Ticket) -> None:
        """Write the ticket's stage, flags, and order, and append its new events (then clear them)."""
        ...

    def history(self, key: str) -> list[TicketEvent]:
        """Everything that happened to the ticket, oldest first."""
        ...

    def add_comment(self, key: str, origin: str, author: str, body: str, at: int) -> int:
        """Store a comment and return its id."""
        ...

    def link_blocks(self, prerequisite: str, dependent: str) -> None:
        """Record that `prerequisite` blocks `dependent`. Not yet in Jira."""
        ...

    def blockers(self, key: str) -> list[str]:
        """The tickets that must merge before this one starts, sorted."""
        ...

    def pending_links(self) -> list[tuple[str, str]]:
        """(prerequisite, dependent) pairs not yet in Jira."""
        ...

    def mark_linked(self, pairs: list[tuple[str, str]]) -> None: ...


class StageMappings(Protocol):
    def status_for(self, project_key: str, stage: str) -> str | None:
        """The project's Jira status mapped to this stage, or None if it has none."""
        ...


class OutboxQueue(Protocol):
    def enqueue_transition(self, ticket_key: str, stage: str, actor: str, now: int) -> int:
        """Queue a move of the Jira ticket to `stage`. Older pending moves of the ticket are superseded."""
        ...


class BoardUnitOfWork(Protocol):
    """One transaction over the board. Commits when the block ends, rolls back if it raises."""

    tickets: TicketRepository
    mappings: StageMappings
    outbox: OutboxQueue

    def __enter__(self) -> "BoardUnitOfWork": ...

    def __exit__(self, exc_type, exc, tb) -> None: ...
