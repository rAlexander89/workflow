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
    pr = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'pr' "
                      "order by id desc limit 1", (key,)).fetchone()
    if pr is None:
        return None
    proc = runner(["gh", "pr", "view", pr[0], "--json", "mergeCommit,state"], Path("."))
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    sha = (data.get("mergeCommit") or {}).get("oid")
    if sha:
        _record(conn, key, f"merge:{sha}")
    return sha


def _workflow_state(repo_slug: str, sha: str, workflow: str, runner: Runner) -> str:
    proc = runner(["gh", "run", "list", "--repo", repo_slug, "--branch", "main", "--limit", "50",
                   "--json", "headSha,name,status,conclusion"], Path("."))
    if proc.returncode != 0:
        return "unknown"
    runs = [r for r in json.loads(proc.stdout or "[]") if r.get("headSha") == sha and r.get("name") == workflow]
    if not runs:
        return "waiting"
    if any((r.get("conclusion") or "").lower() in ("failure", "cancelled", "timed_out", "startup_failure")
           for r in runs):
        return "failed"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    return "success"


def observe_dev(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run) -> str:
    sha = (latest(conn, key, "merge:") or "").removeprefix("merge:")
    if not sha:
        return "waiting"
    state = _workflow_state(repo_slug, sha, DEV_WORKFLOW, runner)
    if state in ("success", "failed"):
        _record(conn, key, f"dev:{state}:{sha}")
    if state == "failed":
        record_failure(conn, key, failure_report(repo_slug, sha, DEV_WORKFLOW, runner))
    return state


def dev_deployed(conn: sqlite3.Connection, key: str) -> bool:
    obs = latest(conn, key, "dev:")
    return bool(obs) and obs.startswith("dev:success:")


def start_prod_deploy(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run) -> bool:
    """Start the Deploy to prod workflow for the commit that is on dev. A person presses the button."""
    obs = latest(conn, key, "dev:")
    if not obs or not obs.startswith("dev:success:"):
        return False
    sha = obs.split(":", 2)[2]
    proc = runner(["gh", "workflow", "run", "deploy-prod.yml", "--repo", repo_slug, "-f", f"commit={sha}"], Path("."))
    if proc.returncode != 0:
        _record(conn, key, f"prod:start-failed:{sha}")
        return False
    _record(conn, key, f"prod:started:{sha}")
    return True


def observe_prod(conn: sqlite3.Connection, key: str, repo_slug: str, runner: Runner = _run) -> str:
    obs = latest(conn, key, "prod:started:")
    if not obs:
        return "waiting"
    sha = obs.split(":", 2)[2]
    state = _workflow_state(repo_slug, sha, PROD_WORKFLOW, runner)
    if state in ("success", "failed"):
        _record(conn, key, f"prod:{state}:{sha}")
    return state


def prod_deployed(conn: sqlite3.Connection, key: str) -> bool:
    obs = latest(conn, key, "prod:")
    return bool(obs) and obs.startswith("prod:success:")


def failure_report(repo_slug: str, sha: str, workflow: str, runner: Runner = _run) -> list[dict]:
    """Why a run failed: each failed job, its GitHub link, and the messages GitHub attached to it."""
    proc = runner(["gh", "run", "list", "--repo", repo_slug, "--branch", "main", "--limit", "50",
                   "--json", "databaseId,headSha,name,url"], Path("."))
    if proc.returncode != 0:
        return []
    runs = [r for r in json.loads(proc.stdout or "[]") if r.get("headSha") == sha and r.get("name") == workflow]
    report = []
    for run in runs:
        if run.get("databaseId") is None:
            continue
        jobs = runner(["gh", "run", "view", str(run["databaseId"]), "--repo", repo_slug, "--json", "jobs"], Path("."))
        for job in json.loads(jobs.stdout or "{}").get("jobs", []):
            if (job.get("conclusion") or "") != "failure":
                continue
            ann = runner(["gh", "api", f"repos/{repo_slug}/check-runs/{job['databaseId']}/annotations"], Path("."))
            messages = [a.get("message", "") for a in json.loads(ann.stdout or "[]")
                        if a.get("annotation_level") == "failure" or "not started" in a.get("message", "")]
            report.append({"step": workflow, "job": job.get("name", ""), "url": job.get("url") or run.get("url", ""),
                           "reasons": messages[:3] or ["no message was attached to this job; open it on GitHub"]})
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
