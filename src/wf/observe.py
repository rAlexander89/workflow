"""The observability bot: watches a merge to GitHub's main and the deploys that follow it.

Signals (all read from GitHub through `gh`):
- merge: the PR is merged, and its merge commit is the commit that reached main
- dev deploy: the "Build and Deploy" workflow ran on that commit and succeeded
- prod deploy: the "Deploy to prod" workflow ran on that commit and succeeded

Each observation is written to the ticket's history, so the pipeline strip can read it.
Starting the prod deploy is a separate action, taken by the Deploy to prod button.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

DEV_WORKFLOW = "Build and Deploy"
PROD_WORKFLOW = "Deploy to prod"

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _record(conn: sqlite3.Connection, key: str, text: str) -> None:
    """Write an observation once. Repeating the same reading on every tick adds nothing."""
    last = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'observe' "
                        "order by id desc limit 1", (key,)).fetchone()
    if last and last[0] == text:
        return
    with conn:
        conn.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
                     "values (?, strftime('%s','now'), 'observer', 'observe', null, null, ?)", (key, text))


def latest(conn: sqlite3.Connection, key: str, prefix: str) -> str | None:
    """The most recent observation whose text starts with prefix, e.g. 'dev:'."""
    rows = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'observe' "
                        "order by id desc", (key,)).fetchall()
    for r in rows:
        if r[0] and r[0].startswith(prefix):
            return r[0]
    return None


def merge_sha(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run) -> str | None:
    """Observe the merge: the commit on main that the PR's merge produced."""
    from wf import forge as _forge
    pr = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'pr' "
                      "order by id desc limit 1", (key,)).fetchone()
    if pr is None:
        return None
    st = _forge.for_url(pr[0], runner=runner).pr_status(pr[0])
    sha = st.get("merge_sha") if st.get("ok") else None
    if sha:
        _record(conn, key, f"merge:{sha}")
    return sha


def _workflow_state(repo_slug: str, sha: str, workflow: str | None, forge) -> str:
    runs = [r for r in forge.runs(repo_slug, "main")
            if r.get("sha") == sha and (workflow is None or r.get("name") == workflow)]
    if not runs:
        return "waiting"
    if any(r.get("conclusion") in ("failure", "cancelled", "timed_out", "startup_failure") for r in runs):
        return "failed"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    return "success"


def _forge_or_github(forge, runner: Runner = _run):
    from wf import forge as _forge
    return forge if forge is not None else _forge.GitHub(runner=runner)


def _workflow_name(forge, name: str) -> str | None:
    # GitHub runs are named by their workflow file; a GitLab pipeline is found by its commit.
    return name if forge.name == "github" else None


def observe_dev(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> str:
    f = _forge_or_github(forge, runner)
    sha = (latest(conn, key, "merge:") or "").removeprefix("merge:")
    if not sha:
        return "waiting"
    state = _workflow_state(repo_slug, sha, _workflow_name(f, DEV_WORKFLOW), f)
    if state in ("success", "failed"):
        _record(conn, key, f"dev:{state}:{sha}")
    if state == "failed":
        record_failure(conn, key, failure_report(repo_slug, sha, _workflow_name(f, DEV_WORKFLOW), runner, forge=f))
    return state


def dev_deployed(conn: sqlite3.Connection, key: str) -> bool:
    obs = latest(conn, key, "dev:")
    return bool(obs) and obs.startswith("dev:success:")


def start_prod_deploy(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> bool:
    """Start the Deploy to prod workflow for the commit that is on dev. A person presses the button."""
    obs = latest(conn, key, "dev:")
    if not obs or not obs.startswith("dev:success:"):
        return False
    sha = obs.split(":", 2)[2]
    if not _forge_or_github(forge, runner).dispatch_prod(repo_slug, sha):
        _record(conn, key, f"prod:start-failed:{sha}")
        return False
    _record(conn, key, f"prod:started:{sha}")
    return True


def observe_prod(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run, forge=None) -> str:
    obs = latest(conn, key, "prod:started:")
    if not obs:
        return "waiting"
    sha = obs.split(":", 2)[2]
    state = _workflow_state(repo_slug, sha, _workflow_name(_forge_or_github(forge, runner), PROD_WORKFLOW), _forge_or_github(forge, runner))
    if state in ("success", "failed"):
        _record(conn, key, f"prod:{state}:{sha}")
    return state


def prod_deployed(conn: sqlite3.Connection, key: str) -> bool:
    obs = latest(conn, key, "prod:")
    return bool(obs) and obs.startswith("prod:success:")


def failure_report(repo_slug: str, sha: str, workflow: str | None, runner: Runner = _run, forge=None) -> list[dict]:
    """Why a run failed: each failed job, its link, and the messages the forge attached to it."""
    f = _forge_or_github(forge, runner)
    report = []
    for run in f.runs(repo_slug, "main"):
        if run.get("sha") != sha or (workflow is not None and run.get("name") != workflow):
            continue
        for job in f.jobs(repo_slug, run["id"]):
            if job.get("conclusion") != "failure":
                continue
            messages = [m for m in f.job_messages(repo_slug, job["id"]) if m]
            report.append({"step": run.get("name") or "deploy", "job": job.get("name", ""),
                           "url": job.get("url") or run.get("url", ""),
                           "reasons": messages[:3] or ["no message was attached to this job; open it on the forge"]})
    return report


def record_failure(conn: sqlite3.Connection, key: str, report: list[dict]) -> None:
    """Keep the failure report on the ticket, once per distinct report."""
    text = json.dumps(report, sort_keys=True)
    last = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'failure' "
                        "order by id desc limit 1", (key,)).fetchone()
    if last and last[0] == text:
        return
    with conn:
        conn.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
                     "values (?, strftime('%s','now'), 'observer', 'failure', null, null, ?)", (key, text))


def failures(conn: sqlite3.Connection, key: str) -> list[dict]:
    """The latest failure report for the ticket, as a list of {step, job, url, reasons}."""
    row = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'failure' "
                       "order by id desc limit 1", (key,)).fetchone()
    return json.loads(row[0]) if row else []
