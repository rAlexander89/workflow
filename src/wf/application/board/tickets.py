"""Ticket commands: the use cases that change a ticket. Each one is a single transaction.

The clock and the unit of work are injected. The rules live in `wf.domain.board.ticket`.
"""

from __future__ import annotations

from typing import Callable

from wf.application.events import EventBus
from wf.application.ports.board import BoardUnitOfWork
from wf.application.tracker.handlers import queue_transition
from wf.domain.board import events
from wf.domain.board.events import TicketEvent
from wf.domain.board.errors import BoardError
from wf.domain.board.ticket import (
    Ticket, check_approval, check_block, check_comment, check_flag, check_perf_request, check_stage,
)


class TicketCommands:
    def __init__(self, unit_of_work: Callable[[], BoardUnitOfWork], clock: Callable[[], float],
                 events: EventBus | None = None):
        self._uow = unit_of_work
        self._clock = clock
        self._events = events or EventBus()

    def _save(self, uow: BoardUnitOfWork, ticket: Ticket) -> None:
        """Save the ticket and its recorded events, then publish them inside the same transaction."""
        self._events.publish(uow.tickets.save(ticket), uow)

    def _now(self) -> int:
        return int(self._clock())

    @staticmethod
    def _load(uow: BoardUnitOfWork, key: str, *, in_scope: bool = False) -> Ticket:
        ticket = uow.tickets.get(key)
        if ticket is None or (in_scope and ticket.removed):
            raise BoardError(f"no ticket {key}")
        return ticket

    def move_stage(self, key: str, stage: str, actor: str, reason: str) -> None:
        """Move a ticket to an internal stage and queue the matching Jira move, if the stage has one."""
        check_stage(stage)
        now = self._now()
        with self._uow() as uow:
            ticket = self._load(uow, key)
            if ticket.move_to(stage, actor, reason, now):
                self._save(uow, ticket)

    def queue_transition(self, key: str, stage: str, actor: str) -> int | None:
        """Queue a move of the real Jira ticket to the status mapped to `stage`. None if the stage has none."""
        with self._uow() as uow:
            ticket = self._load(uow, key)
            return queue_transition(uow, key, ticket.project_key, stage, actor, self._now())

    def set_flag(self, key: str, flag: str, actor: str, reason: str) -> None:
        check_flag(flag)
        with self._uow() as uow:
            ticket = self._load(uow, key)
            if ticket.flag(flag, actor, reason, self._now()):
                self._save(uow, ticket)

    def clear_flag(self, key: str, flag: str, actor: str) -> None:
        if actor != "user":
            raise BoardError("only the user can clear a flag")
        with self._uow() as uow:
            ticket = self._load(uow, key)
            if ticket.clear_flag(flag, actor, self._now()):
                self._save(uow, ticket)

    def add_comment(self, key: str, author: str, body: str, origin: str = "agent") -> int:
        check_comment(body)
        now = self._now()
        with self._uow() as uow:
            ticket = self._load(uow, key)
            comment_id = uow.tickets.add_comment(key, origin, author, body, now)
            ticket.comment(author, body, now)
            self._save(uow, ticket)
            return comment_id

    def record_approval(self, key: str, role: str, head: str) -> None:
        check_approval(role, head)
        with self._uow() as uow:
            ticket = self._load(uow, key)
            ticket.approve(role, head, self._now())
            self._save(uow, ticket)

    def request_perf_check(self, key: str, actor: str, reason: str) -> None:
        check_perf_request(actor)
        with self._uow() as uow:
            ticket = self._load(uow, key)
            ticket.request_perf_check(actor, reason, self._now())
            self._save(uow, ticket)

    def add_block(self, prerequisite: str, dependent: str, actor: str, reason: str) -> None:
        """Record that `prerequisite` must merge before `dependent` starts."""
        check_block(prerequisite, dependent)
        with self._uow() as uow:
            first = self._load(uow, prerequisite, in_scope=True)
            second = self._load(uow, dependent, in_scope=True)
            uow.tickets.link_blocks(prerequisite, dependent)
            second.blocked_by(first, actor, reason, self._now())
            self._save(uow, second)

    def set_order(self, key: str, position: int, actor: str) -> None:
        """Where the ticket sits in the sprint's work order. Lower goes first."""
        with self._uow() as uow:
            ticket = self._load(uow, key, in_scope=True)
            ticket.set_order(position, actor, self._now())
            self._save(uow, ticket)

    def get(self, key: str) -> Ticket | None:
        """The ticket, including one removed from scope, or None if the key is unknown."""
        with self._uow() as uow:
            return uow.tickets.get(key)

    def status_for(self, project_key: str, stage: str) -> str | None:
        """The project's Jira status mapped to this stage, or None."""
        with self._uow() as uow:
            return uow.mappings.status_for(project_key, stage)

    def record_note(self, key: str, actor: str, kind: str, detail: str, *, once: bool = False) -> None:
        """Write a note on the ticket's history (an observation, a PR link). With once=True, repeating the
        newest note of this kind adds nothing."""
        with self._uow() as uow:
            if once and uow.tickets.last_event_detail(key, kind) == detail:
                return
            uow.tickets.append_event(TicketEvent(key, self._now(), actor, kind, None, None, detail))

    def last_note(self, key: str, kind: str) -> str | None:
        with self._uow() as uow:
            return uow.tickets.last_event_detail(key, kind)

    def notes(self, key: str, kind: str) -> list[str]:
        """The notes of this kind, newest first."""
        with self._uow() as uow:
            return uow.tickets.event_details(key, kind)

    # --- questions about a ticket's history -------------------------------------------

    def approvals_at(self, key: str, head: str) -> set[str]:
        with self._uow() as uow:
            return events.approvals_at(uow.tickets.history(key), head)

    def approvals_since_stage(self, key: str, head: str, stage: str) -> set[str]:
        with self._uow() as uow:
            return events.approvals_since_stage(uow.tickets.history(key), head, stage)

    def changed_since(self, key: str, at: int, by_anyone_but: str) -> bool:
        with self._uow() as uow:
            return events.changed_since(uow.tickets.history(key), at, by_anyone_but)

    def perf_requested(self, key: str) -> bool:
        with self._uow() as uow:
            return events.perf_requested(uow.tickets.history(key))

    def blockers(self, key: str) -> list[str]:
        with self._uow() as uow:
            return uow.tickets.blockers(key)

    def pending_links(self) -> list[tuple[str, str]]:
        with self._uow() as uow:
            return uow.tickets.pending_links()

    def mark_linked(self, pairs: list[tuple[str, str]]) -> None:
        with self._uow() as uow:
            uow.tickets.mark_linked(pairs)
