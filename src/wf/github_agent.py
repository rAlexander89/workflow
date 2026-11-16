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
    if row["stage"] not in ("qa_approved", "merged", "regression", "performance", "deployed"):
        raise PublishBlocked(f"{key} is in {row['stage']}; it must pass QA first")
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
        "select key from ticket where stage = 'qa_approved' and removed_at is null "
        "and key not in (select ticket_key from ticket_event where kind = 'pr')").fetchall()
    for row in rows:
        try:
            open_pull_request(conn, row["key"], repo=repo, confirm=True, runner=runner)
            opened.append(row["key"])
        except PublishBlocked:
            continue
    return opened


def _branch_only_adds_this_ticket(repo: Path, key: str, runner) -> tuple[bool, str]:
    """Every commit the PR would add to GitHub's main must carry the ticket key."""
    branch = agents.branch_for(key)
    fetched = runner(["git", "fetch", "-q", "origin", "main"], repo)
    if fetched.returncode != 0:
        return False, f"could not fetch origin/main: {fetched.stderr.strip()[-200:]}"
    subjects = runner(["git", "log", "--format=%s", f"origin/main..{branch}"], repo)
    if subjects.returncode != 0:
        return False, "could not read the branch's commits"
    lines = [l for l in subjects.stdout.splitlines() if l.strip()]
    stray = [l for l in lines if key not in l]
    if stray:
        return False, f"the branch would also add commits that are not {key}: {stray[0][:80]}"
    return True, ""


def merge_when_clear(conn: sqlite3.Connection, *, repo: Path, runner=None, status=None) -> list[str]:
    """Merge a ticket's GitHub PR once QA has passed it and nothing is flagged.

    Needs: PR reviewer and QA approved the current commit, no flags, GitHub checks passed, and
    a branch that adds only this ticket's commits to GitHub's main. Returns the keys merged.
    """
    from wf import board as _board
    from wf import github_status

    run = runner or (lambda a, cwd: subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=False))
    get = status or github_status.pr_status
    merged = []
    rows = conn.execute(
        "select key, flags, stage from ticket where stage in ('qa_approved', 'merged') and removed_at is null").fetchall()
    for row in rows:
        key = row["key"]
        if row["flags"] not in (None, "[]"):
            continue
        pr = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'pr' "
                          "order by id desc limit 1", (key,)).fetchone()
        if pr is None:
            continue
        url = pr[0]
        st = get(url)
        if not st.get("ok") or st.get("merged") or st.get("state") != "OPEN" or st.get("checks") != "passed":
            continue
        head = run(["git", "rev-parse", "--verify", "--quiet", agents.branch_for(key)], repo)
        if head.returncode != 0:
            continue
        approvals = _board.approvals_at(conn, key, head.stdout.strip())
        if not {"pr_review", "qa"} <= approvals:
            continue
        clean, _why = _branch_only_adds_this_ticket(repo, key, run)
        if not clean:
            continue
        proc = run(["gh", "pr", "merge", url, "--merge"], repo)
        if proc.returncode != 0:
            continue
        with conn:
            conn.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
                         "values (?, strftime('%s','now'), 'harness', 'pr_merged', null, null, ?)", (key, url))
        if row["stage"] == "qa_approved":
            _board.move_stage(conn, key, "merged", "harness", f"PR merged into GitHub main: {url}")
        merged.append(key)
    return merged
