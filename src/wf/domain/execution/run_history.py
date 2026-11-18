"""An agent's runs on a ticket, and the rules that read them: whose turn it is, and when to stop trying."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRunRecord:
    """One run of one role at one stage of a ticket."""
    key: str
    stage: str
    role: str
    started_at: int
    exit_code: int | None  # None when the run was stopped for time
    timed_out: bool
    output_path: str

    @property
    def failed(self) -> bool:
        """It timed out, or it did not exit cleanly."""
        return self.timed_out or self.exit_code != 0


# Every function below takes a ticket's runs, newest first.

def at_stage(runs: list[AgentRunRecord], stage: str) -> list[AgentRunRecord]:
    return [r for r in runs if r.stage == stage]


def last_run(runs: list[AgentRunRecord], stage: str) -> AgentRunRecord | None:
    here = at_stage(runs, stage)
    return here[0] if here else None


def last_role_at(runs: list[AgentRunRecord], stage: str) -> str | None:
    run = last_run(runs, stage)
    return None if run is None else run.role


def failed_in_a_row(runs: list[AgentRunRecord], stage: str) -> int:
    """Runs in a row at this stage, newest first, that failed. A run that succeeds ends the count, so work keeps
    going while the agent is making progress. Only repeated failures count toward the stop: that is a real
    obstacle."""
    streak = 0
    for run in at_stage(runs, stage):
        if not run.failed:
            break
        streak += 1
    return streak


def product_rounds(runs: list[AgentRunRecord]) -> int:
    """How many times the product agent has run on the ticket, at any stage."""
    return sum(1 for r in runs if r.role == "product")


def last_exit_code(runs: list[AgentRunRecord], role: str | None = None) -> int | None:
    """The exit code of the newest run (of this role, if given), or None if none ran."""
    for run in runs:
        if role is None or run.role == role:
            return run.exit_code
    return None
