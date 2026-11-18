"""A ticket's internal pull request: its branch, head commit, approvals, and the merge bot's verdict."""

from __future__ import annotations

from dataclasses import dataclass

from wf.domain.delivery.branches import branch_for
from wf.domain.execution import run_history
from wf.entrypoints.web.viewmodels.base import ViewModel


@dataclass
class PrCardViewModel(ViewModel):
    template = "_pr_card.html"
    empty: str = ""  # why there is no card to show: no repo, or no branch yet
    key: str = ""
    branch: str = ""
    head: str = ""
    pr: str | None = None
    summary: str = ""
    approved: str = ""
    last_run: str = ""
    merged: bool = False
    verdict: object = None

    @classmethod
    def load(cls, container, key: str) -> "PrCardViewModel | None":
        """None when there is no such ticket."""
        settings = container.settings()
        with container.session() as s:
            if s.tickets.get(key) is None:
                return None
            if not settings.repos:
                return cls(empty="No repo linked.")
            workspace = s.workspace(settings)
            head = workspace.branch_head(key)
            if head is None:
                return cls(empty="No branch yet. The dev run creates it.")
            stat = workspace.diff_stat(key)
            pr = s.tickets.last_note(key, "pr")
            approvals = sorted(s.tickets.approvals_at(key, head))
            tests = run_history.last_exit_code(s.agent_runs.runs_for(key), "dev")
            bot = s.merge_bot(settings)
            return cls(
                key=key, branch=branch_for(key), head=head, pr=pr,
                summary=stat[-1] if stat else "no changes against main",
                approved=", ".join(approvals) if approvals else "none at this commit",
                last_run="ok" if tests == 0 else "not ok" if tests is not None else "none",
                merged=bool(pr) and bot.is_merged(pr), verdict=bot.assess(key))

    @staticmethod
    def diff(container, key: str) -> str:
        """The ticket's change as a unified diff."""
        settings = container.settings()
        with container.session() as s:
            return s.workspace(settings).diff_text(key) or "no diff"
