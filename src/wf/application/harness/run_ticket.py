"""Run again: a person starts the agent for a ticket's stage by hand."""

from __future__ import annotations

from dataclasses import dataclass

from wf.application.board.tickets import TicketCommands
from wf.application.delivery.pipeline import PipelineService
from wf.application.harness.passes import PassService
from wf.application.ports.persistence import PauseState
from wf.domain.delivery.roles import REFINEMENT_STAGES, STAGE_ROLE


class NotStarted(Exception):
    """The agent was not started; the message says why."""


@dataclass(frozen=True)
class RunPlan:
    key: str
    stage: str
    role: str


class RunTicketService:
    def __init__(self, *, commands: TicketCommands, pipeline: PipelineService, pause: PauseState,
                 passes: PassService):
        self._commands = commands
        self._pipeline = pipeline
        self._pause = pause
        self._passes = passes

    def prepare(self, key: str) -> RunPlan:
        """Check that an agent can be started now, and which one. Raises NotStarted if not."""
        if self._pause.is_paused():
            raise NotStarted("Not started: work is paused. Resume it, then run the agent.")
        idle = self._pipeline.idle_reason(key)
        if idle is not None and not idle["can_run"]:
            raise NotStarted(f"Not started: {idle['why']}")
        ticket = self._commands.get(key)
        if ticket is None or ticket.stage not in STAGE_ROLE:
            raise NotStarted("Not started: no agent works this stage.")
        role = (self._pipeline.refinement_role(key) if ticket.stage in REFINEMENT_STAGES
                else STAGE_ROLE[ticket.stage])
        return RunPlan(key, ticket.stage, role)

    def execute(self, plan: RunPlan) -> int | None:
        """Run the planned agent. Returns its exit code (None if it timed out)."""
        if plan.stage == "ready_for_dev":
            self._commands.move_stage(plan.key, "in_dev", "dev", "dev pickup")
        result = self._passes.run_agent(plan.role, plan.key)
        return result.run.exit_code if result.run else None
