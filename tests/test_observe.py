import json
import subprocess

from wf import board, observe
from tests.test_board import clock, payload, sync, ticket

SHA = "a" * 40


class Gh:
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


def setup(tmp_path):
    c = board.connect(tmp_path / "wf.sqlite")
    sync(c, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    c.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
              "values ('ABC-1', 1, 'harness', 'pr', null, null, 'https://github.com/o/r/pull/9')")
    c.commit()
    return c


def test_merge_commit_is_observed(tmp_path):
    c = setup(tmp_path)
    assert observe.merge_sha(c, "ABC-1", "o/r", runner=Gh()) == SHA
    assert observe.latest(c, "ABC-1", "merge:") == f"merge:{SHA}"


def test_dev_deploy_success_is_observed_on_that_commit(tmp_path):
    c = setup(tmp_path)
    observe.merge_sha(c, "ABC-1", "o/r", runner=Gh())
    runs = [{"headSha": SHA, "name": "Build and Deploy", "status": "completed", "conclusion": "success"}]
    assert observe.observe_dev(c, "ABC-1", "o/r", runner=Gh(runs=runs)) == "success"
    assert observe.dev_deployed(c, "ABC-1")


def test_a_failed_dev_deploy_is_red_and_not_deployed(tmp_path):
    c = setup(tmp_path)
    observe.merge_sha(c, "ABC-1", "o/r", runner=Gh())
    runs = [{"headSha": SHA, "name": "Build and Deploy", "status": "completed", "conclusion": "failure"}]
    assert observe.observe_dev(c, "ABC-1", "o/r", runner=Gh(runs=runs)) == "failed"
    assert not observe.dev_deployed(c, "ABC-1")


def test_a_running_dev_deploy_is_not_yet_deployed(tmp_path):
    c = setup(tmp_path)
    observe.merge_sha(c, "ABC-1", "o/r", runner=Gh())
    runs = [{"headSha": SHA, "name": "Build and Deploy", "status": "in_progress", "conclusion": ""}]
    assert observe.observe_dev(c, "ABC-1", "o/r", runner=Gh(runs=runs)) == "running"
    assert not observe.dev_deployed(c, "ABC-1")


def test_prod_deploy_starts_only_after_dev_succeeded_on_the_same_commit(tmp_path):
    c = setup(tmp_path)
    assert not observe.start_prod_deploy(c, "ABC-1", "o/r", runner=Gh())
    observe.merge_sha(c, "ABC-1", "o/r", runner=Gh())
    observe._record(c, "ABC-1", f"dev:success:{SHA}")
    gh = Gh()
    assert observe.start_prod_deploy(c, "ABC-1", "o/r", runner=gh)
    assert ["gh", "workflow", "run", "deploy-prod.yml", "--repo", "o/r", "-f", f"commit={SHA}"] in gh.calls
