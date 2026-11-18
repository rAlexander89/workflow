"""The merge bot: decides whether a ticket's GitHub PR is good to merge, and says why not.

Good to merge means all of these hold:
- the PR is open on GitHub, not already merged or closed
- the internal PR reviewer and QA both approved the branch's current commit
- nothing is flagged on the ticket
- GitHub's checks have passed
- every commit the PR would add to GitHub's main carries the ticket key

The bot only reads. The auto pass calls merge_when_clear, which merges only when this says yes.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

from tests.support import board
from wf.adapters.forge import github_status as github_status
from wf.adapters.workspace.git import GitWorkspace, run_git
from wf.application.delivery.merge import MergeBot
from wf.domain.delivery.merge_gate import Verdict  # noqa: F401

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


class _Status:
    """PullRequests over a status function, so callers can inject how a PR is read."""

    def __init__(self, status: Callable[[str], dict]):
        self.status = status


def stray_commits(key: str, repo: Path, runner: Runner | None = None) -> tuple[bool, list[str]]:
    """(ok, stray): ok when every commit the branch adds to GitHub's main carries the ticket key."""
    return GitWorkspace(repo, runner=runner or run_git).stray_commits(key)


def assess(conn: sqlite3.Connection, key: str, *, repo: Path, runner: Runner | None = None,
           status: Callable[[str], dict] | None = None) -> Verdict:
    bot = MergeBot(commands=board._commands(conn), workspace=GitWorkspace(repo, runner=runner or run_git),
                   pull_requests=_Status(status or github_status.pr_status))
    return bot.assess(key)
