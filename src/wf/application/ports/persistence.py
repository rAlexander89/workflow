"""Persistence ports: what the application needs from storage, with no database in sight.

Each port is a Protocol. The SQLite adapters in `wf.adapters.persistence.sqlite` implement them,
and the tests use in-memory fakes that pass the same contract tests.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from wf.domain.delivery.routing import Decision
from wf.domain.execution.run import Run
from wf.domain.execution.usage import AgentAudit, UsageRecord
from wf.domain.tracker.changelog import StatusChange


class RunRegistry(Protocol):
    """One record per workflow run, for the run list and status pages."""

    def create(self, run_id: str, request: str, status: str = "running") -> Run: ...

    def set_status(self, run_id: str, status: str) -> None: ...

    def get(self, run_id: str) -> Run | None: ...

    def list(self) -> list[Run]: ...


class UsageLog(Protocol):
    """Every model call an agent makes, and the audit built from them."""

    def record(self, rec: UsageRecord) -> None: ...

    def audit(self) -> list[AgentAudit]: ...

    def largest_calls(self, limit: int = 5) -> list[dict]: ...


class ChangeLog(Protocol):
    """Jira status changes already seen, and when the last webhook arrived."""

    def record_new(self, changes: Iterable[StatusChange]) -> list[StatusChange]: ...

    def last_webhook_at(self) -> float | None: ...

    def mark_webhook(self, when: float | None = None) -> None: ...


class LifecycleLog(Protocol):
    """Every routing decision, the dispatch count per status, and the kill switch."""

    def record(self, change: StatusChange, decision: Decision, dispatched: bool) -> None: ...

    def dispatch_count(self, issue_key: str, status: str) -> int: ...

    def history(self, issue_key: str) -> list[dict]: ...

    def set_kill(self, on: bool) -> None: ...

    def killed(self) -> bool: ...


class PauseState(Protocol):
    """The switch that holds all agent work. Times are Unix seconds, given by the caller."""

    def is_paused(self) -> bool: ...

    def paused_since(self) -> int | None: ...

    def pause(self, reason: str, now: int) -> bool:
        """Turn the pause on. False if work was already paused."""
        ...

    def resume(self, now: int) -> bool:
        """Turn the pause off. False if work was not paused."""
        ...


class AgentRunRepository(Protocol):
    """One record per agent run on a ticket, newest last."""

    def record(self, key: str, stage: str, role: str, started_at: int, exit_code: int | None,
               timed_out: bool, output_path: str) -> None: ...

    def last_role_at(self, key: str, stage: str) -> str | None:
        """The role of the most recent run at this stage, or None if none ran."""
        ...

    def last_run(self, key: str, stage: str) -> dict | None:
        """The most recent run at this stage: role, exit_code, timed_out, started_at."""
        ...

    def failed_in_a_row(self, key: str, stage: str) -> int:
        """Runs in a row, newest first, that exited non-zero or timed out. A success ends the count."""
        ...

    def product_rounds(self, key: str) -> int: ...

    def runs_for(self, key: str) -> list[dict]:
        """All runs on the ticket, newest first."""
        ...

    def last_exit_code(self, key: str, role: str | None = None) -> int | None:
        """The exit code of the newest run (of this role, if given), or None if none ran."""
        ...
