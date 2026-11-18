"""Reactions to board events that belong to the tracker context."""

from __future__ import annotations

from wf.application.ports.board import BoardUnitOfWork
from wf.domain.board.events import TicketMoved


def queue_transition(uow: BoardUnitOfWork, ticket_key: str, project_key: str, stage: str, actor: str,
                     now: int) -> int | None:
    """Queue a move of the real Jira ticket to the status mapped to `stage`. None if the stage has none."""
    if uow.mappings.status_for(project_key, stage) is None:
        return None
    return uow.outbox.enqueue_transition(ticket_key, stage, actor, now)


def on_ticket_moved(event: TicketMoved, uow: BoardUnitOfWork) -> None:
    """A ticket moved on the board: queue the matching move in Jira, if its stage has one."""
    queue_transition(uow, event.ticket_key, event.project_key, event.to_stage, event.actor, event.at)
