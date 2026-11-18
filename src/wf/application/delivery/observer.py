"""Observing what happens after a merge: the merge commit, the dev deploy, and the prod deploy.

Each reading is written once as a note on the ticket ('merge:<sha>', 'dev:success:<sha>', ...), so the
pipeline can ask later without calling the forge again.
"""

from __future__ import annotations

import json
from typing import Callable

from wf.application.board.tickets import TicketCommands
from wf.application.ports.forge import Forge
from wf.domain.delivery import deploys

OBSERVER = "observer"


class DeployObserverService:
    def __init__(self, *, commands: TicketCommands, forge: Forge, forge_for_url: Callable[[str], Forge]):
        self._commands = commands
        self._forge = forge  # where the code is hosted: CI runs and deploys are read from here
        self._forge_for_url = forge_for_url  # a PR link names its own forge

    def _observations(self, key: str) -> list[str]:
        return self._commands.notes(key, "observe")

    def _record(self, key: str, text: str) -> None:
        """Write an observation once. Repeating the same reading on every tick adds nothing."""
        self._commands.record_note(key, OBSERVER, "observe", text, once=True)

    def latest(self, key: str, prefix: str) -> str | None:
        """The most recent observation whose text starts with prefix, e.g. 'dev:'."""
        return deploys.latest(self._observations(key), prefix)

    # --- the merge and the dev deploy --------------------------------------------------------

    def merge_sha(self, key: str) -> str | None:
        """Observe the merge: the commit on main that the PR's merge produced."""
        pr = self._commands.last_note(key, "pr")
        if pr is None:
            return None
        st = self._forge_for_url(pr).pr_status(pr)
        sha = st.get("merge_sha") if st.get("ok") else None
        if sha:
            self._record(key, f"merge:{sha}")
        return sha

    def _state(self, repo_slug: str, sha: str, workflow: str) -> str:
        return deploys.workflow_state(self._forge.runs(repo_slug, "main"), sha,
                                      deploys.workflow_name(self._forge.name, workflow))

    def observe_dev(self, key: str, repo_slug: str) -> str:
        sha = (self.latest(key, "merge:") or "").removeprefix("merge:")
        if not sha:
            return "waiting"
        state = self._state(repo_slug, sha, deploys.DEV_WORKFLOW)
        if state in ("success", "failed", "running"):
            self._record(key, f"dev:{state}:{sha}")
        if state == "success":
            self.record_failure(key, [])  # a passing deploy clears the old failure report
        if state == "failed":
            self.record_failure(key, self.failure_report(
                repo_slug, sha, deploys.workflow_name(self._forge.name, deploys.DEV_WORKFLOW)))
        return state

    def dev_running(self, key: str) -> bool:
        return deploys.is_dev_running(self._observations(key))

    def dev_deployed(self, key: str) -> bool:
        return deploys.is_dev_deployed(self._observations(key))

    # --- prod ----------------------------------------------------------------------------------

    def start_prod_deploy(self, key: str, repo_slug: str) -> bool:
        """Start the Deploy to prod workflow for the commit that is on dev. A person presses the button."""
        note = self.latest(key, "dev:")
        if not note or not note.startswith("dev:success:"):
            return False
        sha = deploys.sha_of(note)
        if not self._forge.dispatch_prod(repo_slug, sha):
            self._record(key, f"prod:start-failed:{sha}")
            return False
        self._record(key, f"prod:started:{sha}")
        return True

    def observe_prod(self, key: str, repo_slug: str) -> str:
        note = self.latest(key, "prod:started:")
        if not note:
            return "waiting"
        sha = deploys.sha_of(note)
        state = self._state(repo_slug, sha, deploys.PROD_WORKFLOW)
        if state in ("success", "failed"):
            self._record(key, f"prod:{state}:{sha}")
        return state

    def prod_deployed(self, key: str) -> bool:
        return deploys.is_prod_deployed(self._observations(key))

    # --- failures ------------------------------------------------------------------------------

    def failure_report(self, repo_slug: str, sha: str, workflow: str | None) -> list[dict]:
        """Why a run failed: each failed job, its link, and the messages the forge attached to it.
        `workflow` is the run's name on this forge, or None to take every run of the commit."""
        report = []
        for run in self._forge.runs(repo_slug, "main"):
            if run.get("sha") != sha or (workflow is not None and run.get("name") != workflow):
                continue
            for job in self._forge.jobs(repo_slug, run["id"]):
                if job.get("conclusion") != "failure":
                    continue
                report.append(deploys.failure_entry(
                    run, job, self._forge.job_messages(repo_slug, job["id"])))
        return report

    def record_failure(self, key: str, report: list[dict]) -> None:
        """Keep the failure report on the ticket, once per distinct report."""
        self._commands.record_note(key, OBSERVER, "failure", json.dumps(report, sort_keys=True), once=True)

    def failures(self, key: str) -> list[dict]:
        """The latest failure report for the ticket, as a list of {step, job, url, reasons}."""
        detail = self._commands.last_note(key, "failure")
        return json.loads(detail) if detail else []
