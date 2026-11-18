"""Publishing: open a real pull request for a ticket that passed the whole internal pipeline, and merge
the ones the merge bot says are good to merge.

Only this service opens a pull request. It needs the ticket to have passed QA, an explicit confirm, and
a remote that is already configured on the repo. It never creates a remote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from wf.application.board.tickets import TicketCommands
from wf.application.delivery.merge import MergeBot
from wf.application.ports.board import BoardQueries
from wf.application.ports.delivery import Workspace
from wf.application.ports.forge import Forge
from wf.domain.board.errors import BoardError
from wf.domain.delivery import deploys
from wf.domain.delivery.branches import branch_for

HARNESS = "harness"
ATTRIBUTION = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


class PublishBlocked(BoardError):
    pass


@dataclass(frozen=True)
class PublishTarget:
    """Where a repo's pull requests go: the forge, the project on it, and whether it has a PR host at all."""
    forge: Forge
    project: str = ""
    hosted: bool = True  # False for a plain local repo: nothing is published


class PublishService:
    def __init__(self, *, commands: TicketCommands, queries: BoardQueries, workspace: Workspace,
                 target: PublishTarget, bot: MergeBot, merge_pr: Callable[[str], bool]):
        self._commands = commands
        self._queries = queries
        self._workspace = workspace
        self._target = target
        self._bot = bot
        self._merge_pr = merge_pr  # merges a PR by URL on whichever forge hosts it

    def open_pull_request(self, key: str, *, confirm: bool) -> str:
        ticket = self._commands.get(key)
        if ticket is None:
            raise PublishBlocked(f"no ticket {key}")
        reason = deploys.publish_stage_block(key, ticket.stage)
        if reason:
            raise PublishBlocked(reason)
        if not confirm:
            raise PublishBlocked("publishing to GitHub needs an explicit confirm")
        if not self._workspace.remote_url():
            raise PublishBlocked("the repo has no origin remote; nothing is created automatically")
        branch = branch_for(key)
        pushed, error = self._workspace.push_branch(key)
        if not pushed:
            raise PublishBlocked(f"could not push {branch}: {error}")
        body = (f"Ticket {key}: {ticket.summary}\n\nPassed the internal pipeline, including regression.\n\n"
                f"{ATTRIBUTION}")
        try:
            url = self._target.forge.open_pr(self._target.project, branch, f"{key}: {ticket.summary}", body)
        except RuntimeError as e:
            raise PublishBlocked(f"could not open the PR: {e}")
        self._commands.record_note(key, HARNESS, "pr", url)
        return url

    def publish_ready(self) -> list[str]:
        """Open the PR for every ticket that has passed QA and has none yet. Local repos have no PR host."""
        if not self._target.hosted:
            return []
        opened = []
        for key in self._queries.qa_approved_without_pr():
            try:
                self.open_pull_request(key, confirm=True)
                opened.append(key)
            except PublishBlocked:
                continue
        return opened

    def merge_when_clear(self) -> list[str]:
        """Merge every ticket whose PR the merge bot says is good to merge. Returns the keys merged."""
        merged = []
        for row in self._queries.keys_in_stages(("qa_approved", "merged")):
            key = row["key"]
            if not self._bot.assess(key).ok:
                continue
            url = self._commands.last_note(key, "pr")
            if not self._merge_pr(url):
                continue
            self._commands.record_note(key, "merge_bot", "pr_merged", url)
            if row["stage"] == "qa_approved":
                self._commands.move_stage(key, "merged", HARNESS, f"PR merged into GitHub main: {url}")
            merged.append(key)
        return merged
