"""Observing the merge and the deploys that follow it, through the Forge port with a fake `gh`."""
import json
import subprocess

import pytest

from tests.builders import make_container, payload, sync, ticket
from wf.bootstrap import Overrides, Session

SHA = "a" * 40
PR = "https://github.com/o/r/pull/9"


class Gh:
    """Stands in for the `gh` command."""
    def __init__(self, runs=None, merge=SHA):
        self.runs, self.merge, self.calls = runs or [], merge, []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        out = ""
        if argv[:3] == ["gh", "pr", "view"]:
            out = json.dumps({"mergeCommit": {"oid": self.merge} if self.merge else None, "state": "MERGED"})
        if argv[:3] == ["gh", "run", "list"]:
            out = json.dumps(self.runs)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def observer_for(tmp_path, repo, conn):
    """An observer whose `gh` is the given fake, over a board with one ticket that has a PR."""
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))

    def build(gh):
        container = make_container(tmp_path, repo=repo, overrides=Overrides(git_runner=gh))
        session = Session(container, conn)
        session.tickets.record_note("ABC-1", "harness", "pr", PR)
        return session.observer(container.settings()), session
    return build


def runs_of(conclusion, status="completed"):
    return [{"headSha": SHA, "name": "Build and Deploy", "status": status, "conclusion": conclusion}]


def test_merge_commit_is_observed(observer_for):
    observer, _ = observer_for(Gh())
    assert observer.merge_sha("ABC-1") == SHA
    assert observer.latest("ABC-1", "merge:") == f"merge:{SHA}"


def test_dev_deploy_success_is_observed_on_that_commit(observer_for):
    observer, _ = observer_for(Gh(runs=runs_of("success")))
    observer.merge_sha("ABC-1")
    assert observer.observe_dev("ABC-1", "o/r") == "success"
    assert observer.dev_deployed("ABC-1")


def test_a_failed_dev_deploy_is_red_and_not_deployed(observer_for):
    observer, _ = observer_for(Gh(runs=runs_of("failure")))
    observer.merge_sha("ABC-1")
    assert observer.observe_dev("ABC-1", "o/r") == "failed"
    assert not observer.dev_deployed("ABC-1")


def test_a_running_dev_deploy_is_not_yet_deployed(observer_for):
    observer, _ = observer_for(Gh(runs=runs_of("", "in_progress")))
    observer.merge_sha("ABC-1")
    assert observer.observe_dev("ABC-1", "o/r") == "running"
    assert not observer.dev_deployed("ABC-1")


def test_prod_deploy_starts_only_after_dev_succeeded_on_the_same_commit(observer_for):
    gh = Gh()
    observer, session = observer_for(gh)
    assert not observer.start_prod_deploy("ABC-1", "o/r")
    observer.merge_sha("ABC-1")
    session.tickets.record_note("ABC-1", "observer", "observe", f"dev:success:{SHA}", once=True)
    assert observer.start_prod_deploy("ABC-1", "o/r")
    assert ["gh", "workflow", "run", "deploy-prod.yml", "--repo", "o/r", "-f", f"commit={SHA}"] in gh.calls
