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

from tests.support import board
from wf.adapters.forge import github_status as github_status
from wf.adapters.persistence.sqlite.board_queries import SqliteBoardQueries
from wf.adapters.workspace.git import GitWorkspace, run_git
from wf.application.delivery.merge import MergeBot
from wf.application.delivery.publish import ATTRIBUTION, PublishBlocked, PublishService, PublishTarget  # noqa: F401


def _load_repo_links():
    from wf.adapters.config import workspace as _cfg
    try:
        return _cfg.load(_cfg.default_path()).repos
    except Exception:
        return []


def _passed_regression(conn: sqlite3.Connection, key: str) -> bool:
    return board.passed_regression(conn, key)


class _Status:
    def __init__(self, status: Callable[[str], dict]):
        self.status = status


def _service(conn: sqlite3.Connection, repo: Path, runner=None, status=None) -> PublishService:
    from wf.adapters.forge import forge as _forge

    run = runner or (lambda a, cwd: subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=False))
    link = next((r for r in _load_repo_links() if Path(r.path) == Path(repo)), None)
    forge_name = link.forge if link else "github"
    forge = _forge.GitHub(runner=lambda argv, cwd: run(argv, cwd)) if forge_name == "github" else \
        _forge.GitLab(host=link.host, runner=lambda argv, cwd: run(argv, cwd))
    workspace = GitWorkspace(repo, runner=run)

    def merge_pr(url: str) -> bool:
        f = _forge.for_url(url)
        return (f.merge_pr(url) if isinstance(f, _forge.GitLab) else
                _forge.GitHub(runner=lambda argv, cwd: run(argv, cwd)).merge_pr(url))

    commands = board._commands(conn)
    bot = MergeBot(commands=commands, workspace=GitWorkspace(repo, runner=run),
                   pull_requests=_Status(status or github_status.pr_status))
    target = PublishTarget(forge, link.remote if link else "", hosted=not (link is not None and link.forge == "local"))
    return PublishService(commands=commands, queries=SqliteBoardQueries(conn), workspace=workspace,
                          target=target, bot=bot, merge_pr=merge_pr)


def open_pull_request(
    conn: sqlite3.Connection, key: str, *, repo: Path, confirm: bool,
    runner: Callable[[list[str], Path], "subprocess.CompletedProcess[str]"] | None = None,
) -> str:
    return _service(conn, repo, runner).open_pull_request(key, confirm=confirm)


def publish_ready(conn: sqlite3.Connection, *, repo: Path, runner=None) -> list[str]:
    """Open the PR for every ticket that has passed QA and has none yet. Local repos have no PR host."""
    return _service(conn, repo, runner).publish_ready()


def merge_when_clear(conn: sqlite3.Connection, *, repo: Path, runner=None, status=None) -> list[str]:
    """Merge every ticket whose PR the merge bot says is good to merge. Returns the keys merged."""
    return _service(conn, repo, runner, status).merge_when_clear()
