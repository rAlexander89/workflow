"""A sprint's state in the tracker. Only an active sprint's tickets are worked."""

from __future__ import annotations

from enum import StrEnum


class SprintState(StrEnum):
    FUTURE = "future"
    ACTIVE = "active"
    CLOSED = "closed"
