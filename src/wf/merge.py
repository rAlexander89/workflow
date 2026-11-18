"""The harness merge gate: merge a ticket's branch into the internal main, only when the
work is approved at its current commit and the tests pass there.

Rules (from docs/plan.md, section 7):
- two approvals from two different roles (pr_review and qa), on the current head
- the tests pass on that head
- the ticket is in `qa_approved`
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from wf import board
from wf.adapters.workspace.git import GitWorkspace, ShellTestRunner
from wf.application.delivery.merge import MergeService
from wf.domain.delivery.merge_gate import MergeBlocked  # noqa: F401
from wf.domain.delivery.roles import REQUIRED_APPROVALS  # noqa: F401


def _service(conn: sqlite3.Connection, repo: Path, runs_dir: Path, clock=None) -> MergeService:
    return MergeService(commands=board._commands(conn, clock or time.time),
                        workspace=GitWorkspace(repo, runs_dir), tests=ShellTestRunner(runs_dir))


def check(conn: sqlite3.Connection, key: str, worktree: Path, test_cmd: str) -> str:
    """Return the head commit if the ticket may merge; raise MergeBlocked otherwise."""
    runs_dir = Path(worktree).parent.parent
    return _service(conn, Path(worktree), runs_dir).check(key, test_cmd)


def merge(conn: sqlite3.Connection, key: str, *, repo: Path, runs_dir: Path, test_cmd: str,
          clock=None) -> str:
    """Merge the approved branch into the internal main and move the ticket to `merged`."""
    return _service(conn, repo, runs_dir, clock).merge(key, test_cmd)


def local_merge_when_clear(conn: sqlite3.Connection, *, repo: Path, runs_dir: Path, test_cmd: str) -> list[str]:
    """For a repo with no hosted remote: merge each QA-approved ticket into the local main."""
    keys = [row["key"] for row in board.keys_in_stages(conn, ("qa_approved",))]
    return _service(conn, repo, runs_dir).merge_local_when_clear(keys, test_cmd)
