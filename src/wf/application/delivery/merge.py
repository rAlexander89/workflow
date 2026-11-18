"""Merging: the internal merge gate, and the merge bot's verdict on a GitHub pull request."""

from __future__ import annotations

from wf.application.board.tickets import TicketCommands
from wf.application.ports.delivery import PullRequests, TestRunner, Workspace
from wf.domain.delivery import merge_gate
from wf.domain.delivery.branches import branch_for
from wf.domain.delivery.merge_gate import MergeBlocked, Verdict

HARNESS = "harness"


class MergeService:
    def __init__(self, *, commands: TicketCommands, workspace: Workspace, tests: TestRunner):
        self._commands = commands
        self._workspace = workspace
        self._tests = tests

    def check(self, key: str, test_cmd: str) -> str:
        """Return the head commit if the ticket may merge; raise MergeBlocked otherwise."""
        ticket = self._commands.get(key)
        if ticket is None:
            raise MergeBlocked(f"no ticket {key}")
        self._raise(merge_gate.stage_block(key, ticket.stage))
        head = self._workspace.worktree_head(key)
        self._raise(merge_gate.approval_block(key, head, self._commands.approvals_at(key, head)))
        self._raise(merge_gate.test_block(head, test_cmd, None))
        result = self._tests.run(key, test_cmd)
        self._raise(merge_gate.test_block(head, test_cmd, result.ok, result.output))
        return head

    @staticmethod
    def _raise(reason: str | None) -> None:
        if reason:
            raise MergeBlocked(reason)

    def merge(self, key: str, test_cmd: str) -> str:
        """Merge the approved branch into the internal main and move the ticket to `merged`."""
        if not self._workspace.worktree_exists(key):
            raise MergeBlocked(f"no worktree for {key}")
        head = self.check(key, test_cmd)
        self._workspace.merge_branch(key)
        self._commands.move_stage(key, "merged", HARNESS, f"merged {branch_for(key)} at {head[:8]}")
        return head

    def merge_local_when_clear(self, qa_approved_keys: list[str], test_cmd: str) -> list[str]:
        """For a repo with no hosted remote: merge each QA-approved ticket into the local main.

        The gate is the same (the required approvals on the head, passing tests). With no PR host there is no
        separate dev deploy, so the local app stands in for dev: the merge is recorded as the dev deploy,
        and regression runs against it.
        """
        merged = []
        for key in qa_approved_keys:
            try:
                self.merge(key, test_cmd)
            except MergeBlocked:
                continue
            sha = self._workspace.main_head()
            self._commands.record_note(key, "observer", "observe", f"merge:{sha}", once=True)
            self._commands.record_note(key, "observer", "observe", f"dev:success:{sha}", once=True)
            merged.append(key)
        return merged


class MergeBot:
    """Decides whether a ticket's GitHub PR is good to merge, and says why not."""

    def __init__(self, *, commands: TicketCommands, workspace: Workspace, pull_requests: PullRequests):
        self._commands = commands
        self._workspace = workspace
        self._prs = pull_requests

    def assess(self, key: str) -> Verdict:
        ticket = self._commands.get(key)
        if ticket is None:
            return merge_gate.verdict(merge_gate.PullRequestFacts(key, ticket_exists=False))
        url = self._commands.last_note(key, "pr")
        if url is None:
            return merge_gate.verdict(merge_gate.PullRequestFacts(key, flagged=bool(ticket.flags)))
        pr = self._prs.status(url)
        head = self._workspace.branch_head(key)
        return merge_gate.verdict(merge_gate.PullRequestFacts(
            key, flagged=bool(ticket.flags), pr_url=url, pr=pr, branch_head=head,
            approved_at_head=frozenset(self._commands.approvals_at(key, head)) if head else frozenset(),
            stray=(lambda r: (r[0], tuple(r[1])))(self._workspace.stray_commits(key)) if head else None))
