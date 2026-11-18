"""Ticket commands: the use cases that change a ticket. Each one is a single transaction.

The clock and the unit of work are injected. The rules live in `wf.domain.board.ticket`.
"""

from __future__ import annotations

from typing import Callable

from wf.application.ports.board import BoardUnitOfWork
from wf.domain.board import events
from wf.domain.board.events import TicketEvent
from wf.domain.board.errors import BoardError
from wf.domain.board.ticket import (
    Ticket, check_approval, check_block, check_comment, check_flag, check_perf_request, check_stage,
)


class TicketCommands:
    def __init__(self, unit_of_work: Callable[[], BoardUnitOfWork], clock: Callable[[], float]):
        self._uow = unit_of_work
        self._clock = clock

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
                uow.tickets.save(ticket)
                self._queue_transition(uow, ticket, stage, actor, now)

    def queue_transition(self, key: str, stage: str, actor: str) -> int | None:
        """Queue a move of the real Jira ticket to the status mapped to `stage`. None if the stage has none."""
        with self._uow() as uow:
            return self._queue_transition(uow, self._load(uow, key), stage, actor, self._now())

    @staticmethod
    def _queue_transition(uow: BoardUnitOfWork, ticket: Ticket, stage: str, actor: str, now: int) -> int | None:
        if uow.mappings.status_for(ticket.project_key, stage) is None:
            return None
        return uow.outbox.enqueue_transition(ticket.key, stage, actor, now)

    def set_flag(self, key: str, flag: str, actor: str, reason: str) -> None:
        check_flag(flag)
        with self._uow() as uow:
            ticket = self._load(uow, key)
            if ticket.flag(flag, actor, reason, self._now()):
                uow.tickets.save(ticket)

    def clear_flag(self, key: str, flag: str, actor: str) -> None:
        if actor != "user":
            raise BoardError("only the user can clear a flag")
        with self._uow() as uow:
            ticket = self._load(uow, key)
            if ticket.clear_flag(flag, actor, self._now()):
                uow.tickets.save(ticket)

    def add_comment(self, key: str, author: str, body: str, origin: str = "agent") -> int:
        check_comment(body)
        now = self._now()
        with self._uow() as uow:
            ticket = self._load(uow, key)
            comment_id = uow.tickets.add_comment(key, origin, author, body, now)
            ticket.comment(author, body, now)
            uow.tickets.save(ticket)
            return comment_id

    def record_approval(self, key: str, role: str, head: str) -> None:
        check_approval(role, head)
        with self._uow() as uow:
            ticket = self._load(uow, key)
            ticket.approve(role, head, self._now())
            uow.tickets.save(ticket)

    def request_perf_check(self, key: str, actor: str, reason: str) -> None:
        check_perf_request(actor)
        with self._uow() as uow:
            ticket = self._load(uow, key)
            ticket.request_perf_check(actor, reason, self._now())
            uow.tickets.save(ticket)

    def add_block(self, prerequisite: str, dependent: str, actor: str, reason: str) -> None:
        """Record that `prerequisite` must merge before `dependent` starts."""
        check_block(prerequisite, dependent)
        with self._uow() as uow:
            first = self._load(uow, prerequisite, in_scope=True)
            second = self._load(uow, dependent, in_scope=True)
            uow.tickets.link_blocks(prerequisite, dependent)
            second.blocked_by(first, actor, reason, self._now())
            uow.tickets.save(second)

    def set_order(self, key: str, position: int, actor: str) -> None:
        """Where the ticket sits in the sprint's work order. Lower goes first."""
        with self._uow() as uow:
            ticket = self._load(uow, key, in_scope=True)
            ticket.set_order(position, actor, self._now())
            uow.tickets.save(ticket)

    def get(self, key: str) -> Ticket | None:
        """The ticket, including one removed from scope, or None if the key is unknown."""
        with self._uow() as uow:
            return uow.tickets.get(key)

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
