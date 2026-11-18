"""One cycle of the auto loop: read Jira if it changed, watch the merge and deploys, link, advance, publish,
merge, and start a pass when a ticket is ready.

The loop itself (waiting, waking on a webhook) belongs to the entrypoint. This is what one wake-up does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from wf.application.delivery.merge import MergeService
from wf.application.delivery.observer import DeployObserverService
from wf.application.delivery.publish import PublishService
from wf.application.harness.jira import JiraService
from wf.application.harness.passes import PassService
from wf.application.ports.board import BoardQueries
from wf.application.ports.harness import ActivityLog, TaskRunner
from wf.application.ports.persistence import PauseState
from wf.application.settings import Settings


@dataclass(frozen=True)
class BackgroundWork:
    """The long actions a cycle may start. Each opens its own board session, so it can run on another thread."""
    jira_sync: Callable[[], None]
    link_pending: Callable[[list[tuple[str, str]]], None]
    run_pass: Callable[[], None]


class HarnessCycle:
    def __init__(self, *, settings: Settings, pause: PauseState, queries: BoardQueries, passes: PassService,
                 jira: JiraService, observer: DeployObserverService, merge: MergeService,
                 publish: PublishService, tasks: TaskRunner, work: BackgroundWork, log: ActivityLog):
        self._settings = settings
        self._pause = pause
        self._queries = queries
        self._passes = passes
        self._jira = jira
        self._observer = observer
        self._merge = merge
        self._publish = publish
        self._tasks = tasks
        self._work = work
        self._log = log

    def observe(self) -> None:
        """Watch the merge and the dev and prod deploys on the forge. Runs every cycle, pass or no pass."""
        slug = self._settings.repo_slug()
        for row in self._queries.keys_in_stages(("merged",)):
            key = row["key"]
            if self._observer.latest(key, "merge:") is None:
                sha = self._observer.merge_sha(key)
                if sha:
                    self._log.log(f"{key}: merged to main at {sha[:8]}")
            state = self._observer.observe_dev(key, slug)
            if state == "success":
                self._log.log(f"{key}: deployed to dev")
            elif state == "failed":
                self._log.log(f"{key}: dev deploy FAILED")
        for row in self._queries.keys_in_stages(("deployed",)):
            if self._observer.observe_prod(row["key"], slug) == "success":
                self._log.log(f"{row['key']}: deployed to prod (observed)")

    def run_once(self, *, jira_changed: bool) -> bool:
        """One wake-up. Returns True if a Jira change is still waiting to be read (the sync could not start)."""
        s = self._settings
        if self._pause.is_paused():
            return jira_changed  # paused: no Jira, merge, GitHub, or agent work starts
        still_changed = jira_changed
        if jira_changed and s.claude_code_access:
            still_changed = not self._tasks.start("jira sync", self._work.jira_sync)  # busy: try again next time
        if not (s.auto_run and s.repos and s.claude_code_access):
            return still_changed
        if self._tasks.busy():
            return still_changed
        link = s.link()
        if link is not None and link.forge == "local":
            keys = [r["key"] for r in self._queries.keys_in_stages(("qa_approved",))]
            for key in self._merge.merge_local_when_clear(keys, s.test_cmd):
                self._log.log(f"{key}: merged into the local main (no hosted remote)")
        self.observe()
        pending = self._jira.pending_links()
        if pending and s.claude_code_access:
            self._tasks.start("link tickets in Jira", lambda pairs=pending: self._work.link_pending(pairs))
        self._passes.advance()
        for key in self._publish.publish_ready():
            self._log.log(f"{key}: GitHub PR opened")
        for key in self._publish.merge_when_clear():
            self._log.log(f"{key}: GitHub PR merged (QA passed, checks green, nothing flagged)")
        if self._passes.ready():
            self._tasks.start("auto pass", self._work.run_pass)
        return still_changed
