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
from wf.domain.delivery.roles import REQUIRED_APPROVALS, missing_approvals  # noqa: F401



class MergeBlocked(board.BoardError):
    pass


def check(conn: sqlite3.Connection, key: str, worktree: Path, test_cmd: str) -> str:
    """Return the head commit if the ticket may merge; raise MergeBlocked otherwise."""
    row = board.get_ticket(conn, key)
    if row is None:
        raise MergeBlocked(f"no ticket {key}")
    if row.stage != "qa_approved":
        raise MergeBlocked(f"{key} is in {row.stage}, not qa_approved")
    head = agents.head_of(worktree)
    missing = missing_approvals(board.approvals_at(conn, key, head))
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


def local_merge_when_clear(conn: sqlite3.Connection, *, repo: Path, runs_dir: Path, test_cmd: str) -> list[str]:
    """For a repo with no hosted remote: merge each QA-approved ticket into the local main.

    The merge gate is the same (three approvals on the head, passing tests). With no PR host there
    is no separate dev deploy, so the local app stands in for dev: the merge is recorded as the
    dev deploy, and regression runs against it.
    """
    from wf import observe

    merged = []
    for row in board.keys_in_stages(conn, ("qa_approved",)):
        key = row["key"]
        try:
            head = merge(conn, key, repo=repo, runs_dir=runs_dir, test_cmd=test_cmd)
        except MergeBlocked:
            continue
        sha = agents._git(["rev-parse", "main"], repo)
        observe._record(conn, key, f"merge:{sha}")
        observe._record(conn, key, f"dev:success:{sha}")
        merged.append(key)
    return merged
