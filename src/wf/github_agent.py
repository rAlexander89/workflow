"""The GitHub agent: the only component that opens a real pull request.

It runs only after the whole internal pipeline has passed for a ticket: the ticket has
to be in `deployed`, and it has to have come through regression (the qa role moved it
from `regression` to `deployed`). It also needs an explicit confirm, and a remote that
is already configured on the repo. It never creates a remote.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

from wf import agents, board

ATTRIBUTION = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


class PublishBlocked(board.BoardError):
    pass


def _passed_regression(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute(
        "select 1 from ticket_event where ticket_key = ? and kind = 'stage' "
        "and from_value = 'regression' and to_value = 'deployed' and actor = 'qa'",
        (key,)).fetchone()
    return row is not None


def open_pull_request(
    conn: sqlite3.Connection, key: str, *, repo: Path, confirm: bool,
    runner: Callable[[list[str], Path], "subprocess.CompletedProcess[str]"] | None = None,
) -> str:
    row = conn.execute("select stage, summary from ticket where key = ?", (key,)).fetchone()
    if row is None:
        raise PublishBlocked(f"no ticket {key}")
    if row["stage"] not in ("ready_for_prod", "deployed"):
        raise PublishBlocked(f"{key} is in {row['stage']}; the pipeline must pass first")
    if not confirm:
        raise PublishBlocked("publishing to GitHub needs an explicit confirm")
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo,
                            capture_output=True, text=True, check=False)
    if remote.returncode != 0 or not remote.stdout.strip():
        raise PublishBlocked("the repo has no origin remote; nothing is created automatically")
    branch = agents.branch_for(key)
    run = runner or (lambda a, cwd: subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=False))
    pushed = run(["git", "push", "-u", "origin", branch], repo)
    if pushed.returncode != 0:
        raise PublishBlocked(f"could not push {branch}: {pushed.stderr.strip()[-300:]}")
    body = f"Ticket {key}: {row['summary']}\n\nPassed the internal pipeline, including regression.\n\n{ATTRIBUTION}"
    argv = ["gh", "pr", "create", "--base", "main", "--head", branch,
            "--title", f"{key}: {row['summary']}", "--body", body]
    proc = run(argv, repo)
    if proc.returncode != 0:
        raise PublishBlocked(f"gh pr create failed: {proc.stderr.strip()}")
    url = proc.stdout.strip().splitlines()[-1]
    with conn:
        conn.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
                     "values (?, strftime('%s','now'), 'harness', 'pr', null, null, ?)", (key, url))
    return url


def publish_ready(conn: sqlite3.Connection, *, repo: Path, runner=None) -> list[str]:
    """Open the GitHub PR for every ticket that has passed the internal pipeline and has none yet."""
    opened = []
    rows = conn.execute(
        "select key from ticket where stage = 'ready_for_prod' and removed_at is null "
        "and key not in (select ticket_key from ticket_event where kind = 'pr')").fetchall()
    for row in rows:
        try:
            open_pull_request(conn, row["key"], repo=repo, confirm=True, runner=runner)
            opened.append(row["key"])
        except PublishBlocked:
            continue
    return opened
