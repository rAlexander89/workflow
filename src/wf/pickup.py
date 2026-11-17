"""Dev pickup: take tickets that are ready for development and start them.

A pickup is two things: the board moves the ticket to `in_dev` as the `dev` role, and the
run starts. This module handles the first part and the plan for the second. It is a dry
run unless `apply=True`, so by default it writes nothing.

The gateway is the only way an agent reaches the board. It binds the role at creation,
so a caller can't claim another role per call.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from wf import board

PICKUP_FROM = "ready_for_dev"
PICKUP_TO = "in_dev"


class GatewayError(board.BoardError):
    pass


class Gateway:
    """The board, as one role. Agents get one of these per run."""

    def __init__(self, conn: sqlite3.Connection, role: str, clock: Callable[[], float] | None = None):
        if not role:
            raise GatewayError("a gateway needs a role")
        self.conn = conn
        self.role = role
        self.clock = clock or time.time

    def get_ticket(self, key: str) -> dict | None:
        return board.ticket_detail(self.conn, key)

    def move(self, key: str, stage: str, reason: str) -> None:
        board.move_stage(self.conn, key, stage, self.role, reason, clock=self.clock)

    def flag(self, key: str, flag: str, reason: str) -> None:
        board.set_flag(self.conn, key, flag, self.role, reason, clock=self.clock)


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


def _candidates(conn: sqlite3.Connection) -> list[dict]:
    return board.ready_candidates(conn, PICKUP_FROM)


def _jira_target(conn: sqlite3.Connection, key: str) -> str | None:
    return board.status_for(conn, board.get_ticket(conn, key).project_key, PICKUP_TO)


def plan_pickup(conn: sqlite3.Connection, apply: bool = False,
                clock: Callable[[], float] | None = None) -> PickupPlan:
    """Plan (and, with apply=True, perform) the pickup of ready tickets."""
    plan = PickupPlan(apply=apply)
    gw = Gateway(conn, "dev", clock=clock)
    for row in _candidates(conn):
        item = PickupItem(
            key=row["key"], summary=row["summary"], sprint=row["sprint_name"],
            jira_target=_jira_target(conn, row["key"]),
        )
        flags = json.loads(row["flags"] or "[]")
        if row["sprint_state"] != "active":
            item.skipped = "not in an active sprint"
        elif flags:
            item.skipped = f"flagged: {', '.join(flags)}"
        elif item.jira_target is None:
            item.skipped = f"no Jira status is mapped to {PICKUP_TO}"
        else:
            if apply:
                gw.move(row["key"], PICKUP_TO, "dev pickup")
        plan.items.append(item)
    return plan
