"""Deploying to prod: the one manual step. A person deploys a ticket that has passed regression."""

from __future__ import annotations

from wf.application.board.tickets import TicketCommands
from wf.application.delivery.observer import DeployObserverService
from wf.application.ports.delivery import Workspace
from wf.domain.board.errors import BoardError
from wf.domain.delivery.pipeline import prod_ready


class DeployRefused(BoardError):
    pass


class DeployService:
    def __init__(self, *, commands: TicketCommands, workspace: Workspace, observer: DeployObserverService):
        self._commands = commands
        self._workspace = workspace
        self._observer = observer

    def prod_ready(self, key: str) -> bool:
        head = self._workspace.branch_head(key)
        if head is None:
            return False
        return prod_ready(
            head, self._commands.approvals_since_stage(key, head, "regression"),
            self._commands.perf_requested(key), self._commands.approvals_since_stage(key, head, "performance"))

    def deploy_to_prod(self, key: str, repo_slug: str) -> None:
        """Start the prod workflow and move the ticket to deployed. Raises DeployRefused with the reason."""
        if not self.prod_ready(key):
            raise DeployRefused("regression has not passed on the current commit")
        if not self._observer.start_prod_deploy(key, repo_slug):
            raise DeployRefused("the prod workflow could not be started")
        self._commands.move_stage(key, "deployed", "user", "deployed to prod by the user")
