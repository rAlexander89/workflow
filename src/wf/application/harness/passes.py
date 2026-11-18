"""A pass of the loop: advance the tickets the harness moves, then run the agent for each ticket that needs one."""

from __future__ import annotations

from wf.application.board.tickets import TicketCommands
from wf.application.delivery.pipeline import PipelineService
from wf.application.execution.agent import AgentRunService
from wf.application.ports.harness import ActivityLog
from wf.application.settings import Settings


class PassService:
    def __init__(self, *, pipeline: PipelineService, commands: TicketCommands, agents: AgentRunService,
                 settings: Settings, log: ActivityLog, runner_secret: str = ""):
        self._pipeline = pipeline
        self._commands = commands
        self._agents = agents
        self._settings = settings
        self._log = log
        self._runner_secret = runner_secret

    def run_agent(self, role: str, key: str):
        s = self._settings
        return self._agents.run(
            role, key, access=s.claude_code_access, model=s.model, test_cmd=s.test_cmd, app_cmd=s.app_cmd,
            app_url=s.app_url, bot_domain=s.bot_domain, bot_credentials=s.bot_credentials,
            regression_url=s.regression_url, runner_secret=self._runner_secret)

    def plan(self, dry_run: bool = True) -> list[dict]:
        """One pass over the board: the role for each ticket that needs one.

        dry_run lists the work without running it. While work is paused, nothing is listed or run.
        """
        actions: list[dict] = []
        for planned in self._pipeline.plan_pass():
            action = planned.to_dict()
            if planned.flag_needs_human:
                if not dry_run:
                    self._commands.set_flag(planned.key, "needs-human", "harness", planned.skipped)
            elif planned.skipped is None and not dry_run:
                if planned.stage == "ready_for_dev":
                    self._commands.move_stage(planned.key, "in_dev", "dev", "dev pickup")
                    action["stage"] = "in_dev"
                res = self.run_agent(planned.role, planned.key)
                action["exit_code"] = res.run.exit_code if res.run else None
            actions.append(action)
        return actions

    def ready(self) -> bool:
        """True when a pass would run an agent."""
        try:
            return any(not a.get("skipped") for a in self.plan(dry_run=True))
        except Exception:
            return False

    def advance(self) -> list[str]:
        moved = self._pipeline.advance()
        for key in moved:
            self._log.log(f"{key}: moved on by the harness")
        return moved

    def run_pass(self) -> list[dict]:
        """Advance what needs no agent, then run the agents. Logs what happened."""
        self.advance()
        actions = self.plan(dry_run=False)
        for a in actions:
            self._log.log(f"{a['key']}: {a['role']} on {a['stage']}"
                          + (f" -> {a['skipped']}" if a.get("skipped") else f" (exit {a.get('exit_code')})"))
        if not actions:
            self._log.log("nothing to run: no ticket is waiting for an agent")
        return actions
