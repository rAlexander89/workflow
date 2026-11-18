"""The observability bot: watches a merge to GitHub's main and the deploys that follow it.

Signals (all read from GitHub through `gh`):
- merge: the PR is merged, and its merge commit is the commit that reached main
- dev deploy: the "Build and Deploy" workflow ran on that commit and succeeded
- prod deploy: the "Deploy to prod" workflow ran on that commit and succeeded

Each observation is written to the ticket's history, so the pipeline strip can read it.
Starting the prod deploy is a separate action, taken by the Deploy to prod button.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

from wf import board
from wf.application.delivery.observer import DeployObserverService
from wf.domain.delivery.deploys import DEV_WORKFLOW, PROD_WORKFLOW, workflow_state  # noqa: F401

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _service(conn: sqlite3.Connection, runner: Runner = _run, forge=None) -> DeployObserverService:
    from wf import forge as _forge

    return DeployObserverService(
        commands=board._commands(conn), forge=forge if forge is not None else _forge.GitHub(runner=runner),
        forge_for_url=lambda url: _forge.for_url(url, runner=runner))


def _record(conn: sqlite3.Connection, key: str, text: str) -> None:
    """Write an observation once. Repeating the same reading on every tick adds nothing."""
    _service(conn)._record(key, text)


def latest(conn: sqlite3.Connection, key: str, prefix: str) -> str | None:
    """The most recent observation whose text starts with prefix, e.g. 'dev:'."""
    return _service(conn).latest(key, prefix)


def merge_sha(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> str | None:
    """Observe the merge: the commit on main that the PR's merge produced."""
    return _service(conn, runner, forge).merge_sha(key)


def observe_dev(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> str:
    return _service(conn, runner, forge).observe_dev(key, repo_slug)


def dev_running(conn: sqlite3.Connection, key: str) -> bool:
    return _service(conn).dev_running(key)


def dev_deployed(conn: sqlite3.Connection, key: str) -> bool:
    return _service(conn).dev_deployed(key)


def start_prod_deploy(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> bool:
    """Start the Deploy to prod workflow for the commit that is on dev. A person presses the button."""
    return _service(conn, runner, forge).start_prod_deploy(key, repo_slug)


def observe_prod(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> str:
    return _service(conn, runner, forge).observe_prod(key, repo_slug)


def prod_deployed(conn: sqlite3.Connection, key: str) -> bool:
    return _service(conn).prod_deployed(key)


def failure_report(repo_slug: str, sha: str, workflow: str | None, runner: Runner = _run, forge=None) -> list[dict]:
    """Why a run failed: each failed job, its link, and the messages the forge attached to it."""
    from wf import forge as _forge

    f = forge if forge is not None else _forge.GitHub(runner=runner)
    return DeployObserverService(commands=None, forge=f, forge_for_url=lambda url: f).failure_report(
        repo_slug, sha, workflow)


def record_failure(conn: sqlite3.Connection, key: str, report: list[dict]) -> None:
    """Keep the failure report on the ticket, once per distinct report."""
    _service(conn).record_failure(key, report)


def failures(conn: sqlite3.Connection, key: str) -> list[dict]:
    """The latest failure report for the ticket, as a list of {step, job, url, reasons}."""
    return _service(conn).failures(key)
