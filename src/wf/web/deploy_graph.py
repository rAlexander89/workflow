"""The dev deploy for a ticket's merge, as a graph of its jobs: what ran, what is running, what failed.

Reads the forge through the repo's config. Results are cached briefly so a page load does not
call GitHub or GitLab every time.
"""

from __future__ import annotations

import time

from wf import config as _cfg
from wf import forge as _forge
from wf import observe

CACHE_SECONDS = 20
_cache: dict[tuple, tuple[float, dict | None]] = {}


def _repo_and_forge():
    try:
        cfg = _cfg.load(_cfg.default_path())
    except Exception:
        return None, None
    if not cfg.repos:
        return None, None
    link = cfg.repos[0]
    if link.forge == "local" or not link.remote:
        return None, None
    f = _forge.GitHub() if link.forge == "github" else _forge.GitLab(host=link.host)
    return link.remote, f


def dev_deploy(conn, key: str) -> dict | None:
    """{'name', 'status', 'url', 'jobs': [...]} for the dev deploy of this ticket's merge commit, or None."""
    sha = (observe.latest(conn, key, "merge:") or "").removeprefix("merge:")
    if not sha:
        return None
    repo, f = _repo_and_forge()
    if repo is None:
        return None
    cached = _cache.get((repo, sha))
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    name = observe.DEV_WORKFLOW if f.name == "github" else None
    runs = [r for r in f.runs(repo, "main") if r.get("sha") == sha and (name is None or r.get("name") == name)]
    result = None
    if runs:
        run = runs[0]
        jobs = f.jobs(repo, run["id"])
        jobs.sort(key=lambda j: j.get("started") or "")
        result = {"name": run.get("name", ""), "url": run.get("url", ""), "status": run.get("status", ""),
                  "conclusion": run.get("conclusion", ""), "jobs": [_state(j) for j in jobs]}
    _cache[(repo, sha)] = (time.time(), result)
    return result


def _state(job: dict) -> dict:
    if job.get("status") in ("in_progress", "queued", "waiting", "pending"):
        state = "running"
    elif job.get("conclusion") == "success":
        state = "success"
    elif job.get("conclusion") in ("failure", "timed_out", "startup_failure"):
        state = "failed"
    elif job.get("conclusion") in ("cancelled", "canceled", "skipped"):
        state = job["conclusion"]
    else:
        state = "waiting"
    return {"name": job.get("name", ""), "state": state, "url": job.get("url", "")}
