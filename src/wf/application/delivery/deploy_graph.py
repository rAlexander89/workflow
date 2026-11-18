"""The dev deploy for a ticket's merge, as a graph of its jobs: what ran, what is running, what failed.

Reads the forge. Results are cached briefly so a page load does not call GitHub or GitLab every time.
"""

from __future__ import annotations

from typing import Callable

from wf.application.delivery.observer import DeployObserverService
from wf.application.ports.forge import Forge
from wf.domain.delivery import deploys

CACHE_SECONDS = 5


class DeployGraphService:
    def __init__(self, *, observer: DeployObserverService, forge: Forge | None, repo: str | None,
                 cache: dict, clock: Callable[[], float]):
        self._observer = observer
        self._forge = forge
        self._repo = repo  # None for a local repo or one with no remote: there is no forge to read
        self._cache = cache
        self._clock = clock

    def dev_deploy(self, key: str) -> dict | None:
        """{'name', 'status', 'url', 'jobs': [...]} for the dev deploy of this ticket's merge commit, or None."""
        sha = (self._observer.latest(key, "merge:") or "").removeprefix("merge:")
        if not sha or self._repo is None or self._forge is None:
            return None
        cached = self._cache.get((self._repo, sha))
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            return cached[1]
        name = deploys.workflow_name(self._forge.name, deploys.DEV_WORKFLOW)
        runs = [r for r in self._forge.runs(self._repo, "main")
                if r.get("sha") == sha and (name is None or r.get("name") == name)]
        result = None
        if runs:
            run = runs[0]
            jobs = self._forge.jobs(self._repo, run["id"])
            jobs.sort(key=lambda j: j.get("started") or "")
            result = {"name": run.get("name", ""), "url": run.get("url", ""), "status": run.get("status", ""),
                      "conclusion": run.get("conclusion", ""), "jobs": [deploys.job_state(j) for j in jobs]}
        self._cache[(self._repo, sha)] = (self._clock(), result)
        return result
