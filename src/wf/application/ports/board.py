"""Board ports: the ticket repository, the Jira outbox queue, the stage mapping, and the unit of work."""

from __future__ import annotations

from typing import Callable, Protocol

from wf.domain.board.events import DomainEvent, TicketEvent
from wf.domain.board.payload import SyncResult
from wf.domain.board.ticket import Ticket


class TicketRepository(Protocol):
    def get(self, key: str) -> Ticket | None:
        """The ticket, including one removed from scope (see `Ticket.removed`), or None if unknown."""
        ...

    def save(self, ticket: Ticket) -> list[DomainEvent]:
        """Write the ticket's stage, flags, and order, and the history record of each event it recorded.
        Returns those events, so the caller can publish them."""
        ...

    def history(self, key: str) -> list[TicketEvent]:
        """Everything that happened to the ticket, oldest first."""
        ...

    def add_comment(self, key: str, origin: str, author: str, body: str, at: int) -> int:
        """Store a comment and return its id."""
        ...

    def last_event_detail(self, key: str, kind: str) -> str | None:
        """The detail of the ticket's newest event of this kind, or None."""
        ...

    def event_details(self, key: str, kind: str) -> list[str]:
        """The details of the ticket's events of this kind, newest first."""
        ...

    def append_event(self, event: TicketEvent) -> None:
        """Record an event that is not a change to the ticket itself (an observation, a PR link)."""
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

    def set_jira_status(self, key: str, status: str) -> None:
        """Record the status Jira shows for the ticket."""
        ...


class StageMappings(Protocol):
    def status_for(self, project_key: str, stage: str) -> str | None:
        """The project's Jira status mapped to this stage, or None if it has none."""
        ...


class OutboxQueue(Protocol):
    """Board changes waiting to be pushed to Jira. Each item is sent at most once."""

    def enqueue_transition(self, ticket_key: str, stage: str, actor: str, now: int) -> int:
        """Queue a move of the Jira ticket to `stage`. Older pending moves of the ticket are superseded."""
        ...

    def pending(self) -> list[dict]:
        """Pending moves, oldest first: id, ticket_key, target_stage, project_key, jira_status."""
        ...

    def get(self, item_id: int) -> dict | None: ...

    def update(self, item_id: int, status: str, now: int, detail: str | None = None,
               attempts_bump: bool = False) -> None: ...

    def mark_sent(self, item_id: int, now: int, response_json: str) -> None: ...

    def listing(self, status: str | None = None) -> list[dict]: ...

    def for_ticket(self, key: str) -> list[dict]:
        """The ticket's queued moves, oldest first."""
        ...

    def pending_count(self) -> int: ...


class BoardUnitOfWork(Protocol):
    """One transaction over the board. Commits when the block ends, rolls back if it raises."""

    tickets: TicketRepository
    mappings: StageMappings
    outbox: OutboxQueue

    def __enter__(self) -> "BoardUnitOfWork": ...

    def __exit__(self, exc_type, exc, tb) -> None: ...


class BoardSync(Protocol):
    """Applies a validated sync payload from the Jira session to the board, in one transaction."""

    def carry_workflow_statuses(self, data: dict) -> dict:
        """Keep the project's workflow statuses from the last sync, which the payload cannot show when a
        status has no tickets. Returns the payload."""
        ...

    def sync_file(self, path, clock: Callable[[], float]) -> SyncResult:
        """Read a payload file and apply it. Raises BoardError if the file cannot be read."""
        ...

    def apply(self, data: dict, clock: Callable[[], float]) -> SyncResult:
        """Raises BoardError, with nothing applied, if the payload is invalid or the sync fails."""
        ...


class BoardQueries(Protocol):
    """Read-side views of the board, as plain dicts."""

    def status_to_stage(self) -> dict[str, str]: ...

    def active_board(self) -> list[dict]: ...

    def backlog(self) -> list[dict]: ...

    def unmapped_stages(self) -> list[dict]: ...

    def sprints(self, state: str | None = None) -> list[dict]: ...

    def ticket_detail(self, key: str) -> dict | None: ...

    def last_sync(self, kind: str | None = None) -> dict | None:
        """The newest sync run (of this kind, if given), or None."""
        ...

    def active_sprint(self) -> dict | None:
        """The first active sprint: id and name."""
        ...

    def sprint_name(self, sprint_id: str) -> str | None: ...

    def ticket_links(self, key: str) -> list[dict]: ...

    def counts(self) -> dict:
        """active_tickets, backlog_tickets, flagged, and unmapped_stages."""
        ...

    def pipeline_rows(self) -> list[dict]:
        """In-scope tickets with a stage: key, summary, stage, flags (list), sort_order, sprint_state."""
        ...

    def pipeline_row(self, key: str) -> dict | None:
        """One in-scope ticket: key, stage (may be None), flags (list), sort_order, sprint_state."""
        ...

    def sprint_backlog_keys(self) -> list[str]:
        """In-scope tickets in the backlog stage of an active sprint, sorted."""
        ...

    def kicked_back_dependents(self, stages: tuple[str, ...]) -> list[dict]:
        """Tickets (key, blocker) in these stages of an active sprint whose prerequisite is back in dev."""
        ...

    def keys_in_stages(self, stages: tuple[str, ...]) -> list[dict]:
        """In-scope tickets (key, stage) in any of these stages."""
        ...

    def stage_by_key(self) -> dict[str, str]:
        """The stage of every in-scope ticket that has one."""
        ...

    def qa_approved_without_pr(self) -> list[str]:
        """In-scope qa_approved tickets with no recorded pull request."""
        ...

    def ready_candidates(self, stage: str) -> list[dict]:
        """In-scope tickets in this stage, with sprint_name and sprint_state, sorted by key."""
        ...

    def database_path(self) -> str:
        """Where the board's database file is, for processes started on the board's behalf."""
        ...
