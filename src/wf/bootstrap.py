"""The composition root: the one place that knows which adapter implements each port.

Entrypoints (the CLI, the web app, the gateway, the webhooks) ask the Container for settings and for a
Session. A Session is one board connection and the use cases built over it. Use cases receive their
ports; nothing below this module constructs an adapter.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from wf.adapters.config import workspace as workspace_config
from wf.adapters.forge import forge as forges
from wf.adapters.forge import github_status
from wf.adapters.persistence.sqlite.agent_runs import SqliteAgentRuns
from wf.adapters.persistence.sqlite.board import SqliteBoardUnitOfWork
from wf.adapters.persistence.sqlite.board_queries import SqliteBoardQueries
from wf.adapters.persistence.sqlite.board_sync import SqliteBoardSync
from wf.adapters.persistence.sqlite.pause import SqlitePauseState
from wf.adapters.persistence.sqlite.schema import connect
from wf.adapters.runner import claude_code
from wf.adapters.runner.claude_code import ClaudeCodeRunner
from wf.adapters.runner.prompts import FilePromptLibrary
from wf.adapters.tracker import jira_session
from wf.adapters.workspace.git import GitWorkspace, ShellTestRunner
from wf.application.board.tickets import TicketCommands
from wf.application.delivery.merge import MergeBot, MergeService
from wf.application.delivery.observer import DeployObserverService
from wf.application.delivery.pickup import PickupService, RoleGateway
from wf.application.delivery.pipeline import PipelineService
from wf.application.delivery.publish import PublishService, PublishTarget
from wf.application.execution.agent import AgentRunService
from wf.application.harness.cycle import BackgroundWork, HarnessCycle
from wf.application.harness.deploy import DeployService
from wf.application.harness.jira import JiraService
from wf.application.harness.passes import PassService
from wf.application.harness.run_ticket import RunTicketService
from wf.application.ports.forge import Forge
from wf.application.ports.harness import ActivityLog, TaskRunner
from wf.application.settings import RepoSettings, Settings
from wf.application.tracker.outbox import OutboxService

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
GATEWAY_COMMAND = [sys.executable, "-m", "wf.gateway_server"]


class NullLog:
    def log(self, message: str) -> None:
        pass


class ClaudeSessionTracker:
    """The Tracker port over the user's own Claude session, which holds the Jira connection."""

    def __init__(self, **session: object):
        self._kw = session

    def fetch_sync_payload(self) -> dict:
        return jira_session.fetch_sync_payload(**self._kw)

    def create_blocking_links(self, pairs):
        return jira_session.create_blocking_links(pairs, **self._kw)

    def move_tickets_to_sprint(self, keys, sprint_id):
        return jira_session.move_tickets_to_sprint(keys, sprint_id, **self._kw)

    def move_to_sprint(self, key, sprint_id):
        jira_session.move_to_sprint(key, sprint_id, **self._kw)

    def fetch_observations(self, keys):
        return jira_session.fetch_observations(keys, **self._kw)

    def execute_sends(self, sends):
        return jira_session.execute_sends(sends, **self._kw)


class Session:
    """One board connection and the use cases over it. Close it (or leave its `with` block) when done."""

    def __init__(self, container: "Container", conn: sqlite3.Connection):
        self._c = container
        self.conn = conn
        self.tickets = TicketCommands(lambda: SqliteBoardUnitOfWork(conn), container.clock)
        self.queries = SqliteBoardQueries(conn)
        self.pause = SqlitePauseState(conn)
        self.agent_runs = SqliteAgentRuns(conn)
        self.sync = SqliteBoardSync(conn)
        self.outbox = OutboxService(lambda: SqliteBoardUnitOfWork(conn), container.clock)

    # --- what depends on the linked repo -----------------------------------------------------------

    def _workspace(self, s: Settings, repo: Path | None = None) -> GitWorkspace:
        return GitWorkspace(repo or s.repo_path(), runs_dir=s.runs_dir)

    def forge(self, s: Settings) -> Forge:
        """The forge the linked repo is hosted on: GitHub, or GitLab at its host (GitHub for a local repo)."""
        link = s.link()
        if link is not None and link.forge == "gitlab":
            return forges.GitLab(host=link.host)
        return forges.GitHub()

    def observer(self, s: Settings) -> DeployObserverService:
        return DeployObserverService(commands=self.tickets, forge=self.forge(s), forge_for_url=forges.for_url)

    def pipeline(self, s: Settings) -> PipelineService:
        return PipelineService(commands=self.tickets, queries=self.queries, pause=self.pause,
                               runs=self.agent_runs, workspace=self._workspace(s), deploys=self.observer(s))

    def agents(self, s: Settings) -> AgentRunService:
        return AgentRunService(
            commands=self.tickets, queries=self.queries, runs=self.agent_runs, workspace=self._workspace(s),
            runner=ClaudeCodeRunner(gateway_command=GATEWAY_COMMAND, claude_bin=self._c.claude_bin()),
            prompts=FilePromptLibrary(PROMPTS_DIR), runs_dir=s.runs_dir, clock=self._c.clock)

    def passes(self, s: Settings) -> PassService:
        return PassService(pipeline=self.pipeline(s), commands=self.tickets, agents=self.agents(s), settings=s,
                           log=self._c.log, runner_secret=self._c.runner_secret)

    def run_ticket(self, s: Settings) -> RunTicketService:
        return RunTicketService(commands=self.tickets, pipeline=self.pipeline(s), pause=self.pause,
                                passes=self.passes(s))

    def merge(self, s: Settings) -> MergeService:
        return MergeService(commands=self.tickets, workspace=self._workspace(s), tests=ShellTestRunner(s.runs_dir))

    def merge_bot(self, s: Settings) -> MergeBot:
        return MergeBot(commands=self.tickets, workspace=self._workspace(s),
                        pull_requests=_PullRequests(github_status.pr_status))

    def publish(self, s: Settings) -> PublishService:
        link = s.link()
        target = PublishTarget(self.forge(s), link.remote if link else "",
                               hosted=not (link is not None and link.forge == "local"))

        def merge_pr(url: str) -> bool:
            return forges.for_url(url).merge_pr(url)

        return PublishService(commands=self.tickets, queries=self.queries, workspace=self._workspace(s),
                              target=target, bot=self.merge_bot(s), merge_pr=merge_pr)

    def deploy(self, s: Settings) -> DeployService:
        return DeployService(commands=self.tickets, workspace=self._workspace(s), observer=self.observer(s))

    def tracker(self, s: Settings) -> ClaudeSessionTracker:
        return ClaudeSessionTracker(access=s.claude_code_access, workdir=s.runs_dir / "jira-session",
                                    model=s.model, claude_bin=self._c.claude_bin())

    def jira(self, s: Settings) -> JiraService:
        return JiraService(tracker=self.tracker(s), sync=self.sync, queries=self.queries, commands=self.tickets,
                           outbox=self.outbox, log=self._c.log, clock=self._c.clock)

    def pickup(self) -> PickupService:
        return PickupService(commands=self.tickets, queries=self.queries,
                             dev=RoleGateway(self.tickets, self.queries, "dev"))

    def cycle(self, s: Settings, tasks: TaskRunner) -> HarnessCycle:
        return HarnessCycle(
            settings=s, pause=self.pause, queries=self.queries, passes=self.passes(s), jira=self.jira(s),
            observer=self.observer(s), merge=self.merge(s), publish=self.publish(s), tasks=tasks,
            work=self._c.background_work(s), log=self._c.log)


class _PullRequests:
    def __init__(self, status: Callable[[str], dict]):
        self.status = status


class Container:
    def __init__(self, db_path: Path, config_path: Path, *, clock: Callable[[], float] = time.time,
                 log: ActivityLog | None = None, runner_secret: str | None = None,
                 claude_bin: Callable[[], str | None] | None = None):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.clock = clock
        self.log: ActivityLog = log or NullLog()
        self.runner_secret = os.environ.get("WF_RUNNER_SECRET", "") if runner_secret is None else runner_secret
        self.claude_bin = claude_bin or (lambda: shutil.which(claude_code.CLAUDE_BIN))

    def config(self) -> workspace_config.Config:
        return workspace_config.load(self.config_path)

    def settings(self) -> Settings:
        c = self.config()
        return Settings(
            repos=tuple(RepoSettings(r.name, r.path, r.remote, r.forge, r.host) for r in c.repos),
            runs_dir=Path(os.path.expanduser(c.agents.runs_dir)).resolve(), test_cmd=c.agents.test_cmd,
            app_cmd=c.agents.app_cmd, app_url=c.agents.app_url, regression_url=c.agents.regression_url,
            model=c.agents.model, claude_code_access=c.model.claude_code_access, auto_run=c.agents.auto_run,
            bot_domain=c.bots.domain if c.bots.enabled else "", bot_credentials=c.bots.credentials_file)

    @contextmanager
    def session(self) -> Iterator[Session]:
        conn = connect(self.db_path)
        try:
            yield Session(self, conn)
        finally:
            conn.close()

    def background_work(self, s: Settings) -> BackgroundWork:
        """Long actions that each open their own session, so they can run on a worker thread."""

        def jira_sync() -> None:
            with self.session() as session:
                session.jira(s).sync()

        def link_pending(pairs: list[tuple[str, str]]) -> None:
            with self.session() as session:
                session.jira(s).link_pending(pairs)

        def run_pass() -> None:
            with self.session() as session:
                session.passes(s).run_pass()

        return BackgroundWork(jira_sync=jira_sync, link_pending=link_pending, run_pass=run_pass)
