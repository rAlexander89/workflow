"""The workflow engine port: start a run from a request, pause it for a decision, and resume it."""

from __future__ import annotations

from typing import Protocol


class WorkflowEngine(Protocol):
    def start(self, run_id: str, request: str) -> dict:
        """Start a run. Returns its status and what it recorded so far."""
        ...

    def resume(self, run_id: str, approved: bool) -> dict:
        """Continue a run that is waiting for a decision."""
        ...

    def snapshot(self, run_id: str) -> dict:
        """The run's status and what it has recorded, without changing it."""
        ...
