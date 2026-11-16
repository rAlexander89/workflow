"""The harness merge gate: merge a ticket's branch into the internal main, only when the
work is approved at its current commit and the tests pass there.

Rules (from docs/plan.md, section 7):
- two approvals from two different roles (pr_review and qa), on the current head
- the tests pass on that head
- the ticket is in `qa_approved`
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from wf import agents, board

REQUIRED_APPROVALS = {"pr_review", "qa"}


class MergeBlocked(board.BoardError):
    pass


def check(conn: sqlite3.Connection, key: str, worktree: Path, test_cmd: str) -> str:
    """Return the head commit if the ticket may merge; raise MergeBlocked otherwise."""
    row = conn.execute("select stage from ticket where key = ?", (key,)).fetchone()
    if row is None:
        raise MergeBlocked(f"no ticket {key}")
    if row["stage"] != "qa_approved":
        raise MergeBlocked(f"{key} is in {row['stage']}, not qa_approved")
    head = agents.head_of(worktree)
    missing = REQUIRED_APPROVALS - board.approvals_at(conn, key, head)
    if missing:
        raise MergeBlocked(f"{key} at {head[:8]} is missing approval from {', '.join(sorted(missing))}")
    if not test_cmd:
        raise MergeBlocked("no test command is configured, so the tests cannot be checked")
    proc = subprocess.run(test_cmd, shell=True, cwd=worktree, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise MergeBlocked(f"tests fail at {head[:8]}: {(proc.stdout + proc.stderr)[-500:]}")
    return head


def merge(conn: sqlite3.Connection, key: str, *, repo: Path, runs_dir: Path, test_cmd: str,
          clock=None) -> str:
    """Merge the approved branch into the internal main and move the ticket to `merged`."""
    worktree = runs_dir / "worktrees" / key
    if not worktree.exists():
        raise MergeBlocked(f"no worktree for {key}")
    head = check(conn, key, worktree, test_cmd)
    branch = agents.branch_for(key)
    agents._git(["checkout", "main"], repo)
    agents._git(["merge", "--no-ff", branch, "-m", f"Merge {branch} ({key})"], repo)
    kwargs = {"clock": clock} if clock else {}
    board.move_stage(conn, key, "merged", "harness", f"merged {branch} at {head[:8]}", **kwargs)
    return head
