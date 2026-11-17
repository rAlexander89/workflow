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


def _load_repo_links():
    from wf import config as _cfg
    try:
        return _cfg.load(_cfg.default_path()).repos
    except Exception:
        return []


def _passed_regression(conn: sqlite3.Connection, key: str) -> bool:
    return board.passed_regression(conn, key)


def open_pull_request(
    conn: sqlite3.Connection, key: str, *, repo: Path, confirm: bool,
    runner: Callable[[list[str], Path], "subprocess.CompletedProcess[str]"] | None = None,
) -> str:
    row = board.get_ticket(conn, key)
    if row is None:
        raise PublishBlocked(f"no ticket {key}")
    if row.stage not in ("qa_approved", "merged", "regression", "performance", "deployed"):
        raise PublishBlocked(f"{key} is in {row.stage}; it must pass QA first")
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
    body = f"Ticket {key}: {row.summary}\n\nPassed the internal pipeline, including regression.\n\n{ATTRIBUTION}"
    from wf import forge as _forge
    link = next((r for r in _load_repo_links() if Path(r.path) == Path(repo)), None)
    forge_name = link.forge if link else "github"
    f = _forge.GitHub(runner=lambda argv, cwd: run(argv, cwd)) if forge_name == "github" else \
        _forge.GitLab(host=link.host, runner=lambda argv, cwd: run(argv, cwd))
    try:
        url = f.open_pr(link.remote if link else "", branch, f"{key}: {row.summary}", body)
    except RuntimeError as e:
        raise PublishBlocked(f"could not open the PR: {e}")
    board.record_event(conn, key, "harness", "pr", url)
    return url


def publish_ready(conn: sqlite3.Connection, *, repo: Path, runner=None) -> list[str]:
    """Open the PR for every ticket that has passed QA and has none yet. Local repos have no PR host."""
    link = next((r for r in _load_repo_links() if Path(r.path) == Path(repo)), None)
    if link is not None and link.forge == "local":
        return []
    opened = []
    for key in board.qa_approved_without_pr(conn):
        try:
            open_pull_request(conn, key, repo=repo, confirm=True, runner=runner)
            opened.append(key)
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
    """Merge every ticket whose PR the merge bot says is good to merge. Returns the keys merged."""
    from wf import board as _board, merge_bot

    run = runner or (lambda a, cwd: subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=False))
    merged = []
    for row in _board.keys_in_stages(conn, ("qa_approved", "merged")):
        key = row["key"]
        verdict = merge_bot.assess(conn, key, repo=repo, runner=run, status=status)
        if not verdict.ok:
            continue
        url = _board.last_event_detail(conn, key, "pr")
        from wf import forge as _forge
        f = _forge.for_url(url)
        merged_ok = (f.merge_pr(url) if isinstance(f, _forge.GitLab) else
                     _forge.GitHub(runner=lambda argv, cwd: run(argv, cwd)).merge_pr(url))
        if not merged_ok:
            continue
        _board.record_event(conn, key, "merge_bot", "pr_merged", url)
        if row["stage"] == "qa_approved":
            _board.move_stage(conn, key, "merged", "harness", f"PR merged into GitHub main: {url}")
        merged.append(key)
    return merged
