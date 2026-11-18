"""Running an agent: prepare the ticket's worktree, brief the role, run it, and record the run."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from wf.application.board.tickets import TicketCommands
from wf.application.ports.board import BoardQueries
from wf.application.ports.delivery import Workspace
from wf.application.ports.execution import AgentRunner, PromptLibrary
from wf.application.ports.persistence import AgentRunRepository
from wf.domain.board.errors import BoardError
from wf.domain.execution import agent_run
from wf.domain.execution.agent_run import AgentResult, AgentRunRequest
from wf.domain.execution.run_history import AgentRunRecord


class AgentRunService:
    def __init__(self, *, commands: TicketCommands, queries: BoardQueries, runs: AgentRunRepository,
                 workspace: Workspace, runner: AgentRunner, prompts: PromptLibrary, runs_dir: Path,
                 clock: Callable[[], float]):
        self._commands = commands
        self._queries = queries
        self._runs = runs
        self._workspace = workspace
        self._runner = runner
        self._prompts = prompts
        self._runs_dir = Path(runs_dir)
        self._clock = clock

    def system_prompt(self, role: str, bot_email: str = "", credentials_path: str = "") -> str:
        return agent_run.compose_system_prompt(self._prompts.role_prompt(role), bot_email, credentials_path)

    def run(self, role: str, key: str, *, access: bool, model: str, test_cmd: str = "", app_cmd: str = "",
            app_url: str = "", bot_domain: str = "", bot_credentials: str = "", regression_url: str = "",
            runner_secret: str = "", timeout: float = 3600.0) -> AgentResult:
        ticket = self._commands.get(key)
        on_dev = bool(regression_url) and ticket is not None and ticket.stage == "regression"
        if on_dev:
            app_url = regression_url  # the final regression runs against the deployed dev app
        bot_email = f"{role}@{bot_domain}" if bot_domain else ""
        cred_path = bot_credentials if bot_domain else ""
        detail = self._queries.ticket_detail(key)
        if detail is None:
            raise BoardError(f"no ticket {key}")
        stage = detail["ticket"]["stage"]
        worktree = self._workspace.ensure_worktree(key)
        if role == "dev":
            try:
                self._workspace.sync_with_main(key)
            except RuntimeError as e:
                self._commands.set_flag(key, "blocked", "harness", str(e))
                return AgentResult(key, stage, role, None)
        now = int(self._clock())
        result = self._runner.run(AgentRunRequest(
            worktree=worktree, prompt=agent_run.brief(detail),
            system_prompt=self.system_prompt(role, bot_email, cred_path), model=model,
            run_dir=self._runs_dir / "runs" / key / f"{role}-{now}", role=role, access=access, timeout=timeout,
            gateway_env=agent_run.gateway_env(
                role=role, key=key, db_path=self._queries.database_path(), worktree=worktree, test_cmd=test_cmd,
                app_cmd=app_cmd, app_url=app_url, bot_email=bot_email, credentials_path=cred_path,
                regression_url=regression_url, on_dev=on_dev, runner_secret=runner_secret)))
        self._runs.record(AgentRunRecord(key, stage, role, int(self._clock()), result.exit_code, result.timed_out,
                                         str(result.output_path)))
        return AgentResult(key, stage, role, result)
