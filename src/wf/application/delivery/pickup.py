"""Dev pickup: take tickets that are ready for development and start them.

A pickup is two things: the board moves the ticket to `in_dev` as the `dev` role, and the run starts.
This module handles the first part and the plan for the second. It is a dry run unless `apply=True`,
so by default it writes nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from wf.application.board.tickets import TicketCommands
from wf.application.ports.board import BoardQueries
from wf.domain.board.errors import BoardError
from wf.domain.delivery.pickup import PICKUP_FROM, PICKUP_TO, skip_reason


class GatewayError(BoardError):
    pass


class RoleGateway:
    """The board, as one role. Agents get one of these per run."""

    def __init__(self, commands: TicketCommands, queries: BoardQueries, role: str):
        if not role:
            raise GatewayError("a gateway needs a role")
        self.role = role
        self._commands = commands
        self._queries = queries

    def get_ticket(self, key: str) -> dict | None:
        return self._queries.ticket_detail(key)

    def move(self, key: str, stage: str, reason: str) -> None:
        self._commands.move_stage(key, stage, self.role, reason)

    def flag(self, key: str, flag: str, reason: str) -> None:
        self._commands.set_flag(key, flag, self.role, reason)


@dataclass
class PickupItem:
    key: str
    summary: str
    sprint: str | None
    jira_target: str | None  # the Jira status the move would queue, if any
    skipped: str | None = None  # why it is not picked up, when set


@dataclass
class PickupPlan:
    apply: bool
    items: list[PickupItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "apply": self.apply,
            "pick": [i.__dict__ for i in self.items if i.skipped is None],
            "skipped": [i.__dict__ for i in self.items if i.skipped is not None],
        }


class PickupService:
    def __init__(self, *, commands: TicketCommands, queries: BoardQueries, dev: RoleGateway):
        self._commands = commands
        self._queries = queries
        self._dev = dev

    def plan(self, apply: bool = False) -> PickupPlan:
        """Plan (and, with apply=True, perform) the pickup of ready tickets."""
        plan = PickupPlan(apply=apply)
        for row in self._queries.ready_candidates(PICKUP_FROM):
            ticket = self._commands.get(row["key"])
            item = PickupItem(key=row["key"], summary=row["summary"], sprint=row["sprint_name"],
                              jira_target=self._commands.status_for(ticket.project_key, PICKUP_TO))
            item.skipped = skip_reason(row["sprint_state"], json.loads(row["flags"] or "[]"), item.jira_target)
            if item.skipped is None and apply:
                self._dev.move(row["key"], PICKUP_TO, "dev pickup")
            plan.items.append(item)
        return plan
