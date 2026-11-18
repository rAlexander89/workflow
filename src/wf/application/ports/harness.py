"""Harness ports: the activity log, background tasks, and the tracker."""

from __future__ import annotations

from typing import Callable, Protocol


class ActivityLog(Protocol):
    def log(self, message: str) -> None:
        """One line for the person watching what the harness is doing."""
        ...


class TaskRunner(Protocol):
    """Runs one long action at a time in the background."""

    def start(self, label: str, work: Callable[[], None]) -> bool:
        """Start the work. False if another action is already running."""
        ...

    def busy(self) -> bool: ...


class Tracker(Protocol):
    """The issue tracker (Jira), read and written through the user's own session."""

    def fetch_sync_payload(self) -> dict: ...

    def create_blocking_links(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Returns the pairs it linked."""
        ...

    def move_tickets_to_sprint(self, keys: list[str], sprint_id: str) -> list[str]:
        """Returns the keys it moved."""
        ...

    def move_to_sprint(self, key: str, sprint_id: str) -> None: ...

    def fetch_observations(self, keys: list[str]) -> dict: ...

    def execute_sends(self, sends: list[dict]) -> list[dict]: ...
