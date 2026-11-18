"""Dev pickup: take tickets that are ready for development and start them.

A pickup is two things: the board moves the ticket to `in_dev` as the `dev` role, and the
run starts. This module handles the first part and the plan for the second. It is a dry
run unless `apply=True`, so by default it writes nothing.

The gateway is the only way an agent reaches the board. It binds the role at creation,
so a caller can't claim another role per call.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from wf import board
from wf.adapters.persistence.sqlite.board_queries import SqliteBoardQueries
from wf.application.delivery.pickup import (  # noqa: F401
    GatewayError, PickupItem, PickupPlan, PickupService, RoleGateway,
)
from wf.domain.delivery.pickup import PICKUP_FROM, PICKUP_TO  # noqa: F401


class Gateway(RoleGateway):
    """The board, as one role, over a board connection."""

    def __init__(self, conn: sqlite3.Connection, role: str, clock: Callable[[], float] | None = None):
        super().__init__(board._commands(conn, clock or time.time), SqliteBoardQueries(conn), role)
        self.conn = conn
        self.clock = clock or time.time


def plan_pickup(conn: sqlite3.Connection, apply: bool = False,
                clock: Callable[[], float] | None = None) -> PickupPlan:
    """Plan (and, with apply=True, perform) the pickup of ready tickets."""
    commands = board._commands(conn, clock or time.time)
    return PickupService(commands=commands, queries=SqliteBoardQueries(conn),
                         dev=Gateway(conn, "dev", clock)).plan(apply)
