"""Deploy observation and publishing, with fake forge, workspace, and board."""
import json

import pytest

from tests.fakes import MemoryBoardQueries, MemoryBoardState, MemoryBoardUnitOfWork
from wf.application.board.tickets import TicketCommands
from wf.application.delivery.merge import MergeBot
from wf.application.delivery.observer import DeployObserverService
from wf.application.delivery.publish import PublishBlocked, PublishService, PublishTarget
from wf.domain.delivery import deploys

SHA = "f" * 40


def test_the_latest_note_with_a_prefix_wins_and_states_are_read_from_it():
    notes = ["dev:success:" + SHA, "dev:running:" + SHA, "merge:" + SHA]  # newest first
    assert deploys.latest(notes, "dev:") == "dev:success:" + SHA
    assert deploys.is_dev_deployed(notes) and not deploys.is_dev_running(notes)
    assert deploys.is_dev_running(["dev:running:x"]) and not deploys.is_dev_deployed([])
    assert deploys.is_prod_deployed(["prod:success:x"]) and not deploys.is_prod_deployed(["prod:failed:x"])
    assert deploys.sha_of("prod:started:abc") == "abc"


def test_workflow_state_from_runs():
    run = lambda **kw: {"sha": SHA, "name": "Build and Deploy", "status": "completed", "conclusion": "success", **kw}  # noqa: E731
    assert deploys.workflow_state([], SHA, None) == "waiting"
    assert deploys.workflow_state([run(sha="other")], SHA, None) == "waiting"
    assert deploys.workflow_state([run(name="Other")], SHA, "Build and Deploy") == "waiting"
    assert deploys.workflow_state([run()], SHA, "Build and Deploy") == "success"
    assert deploys.workflow_state([run(), run(status="in_progress")], SHA, None) == "running"
    assert deploys.workflow_state([run(conclusion="failure"), run(status="in_progress")], SHA, None) == "failed"
    assert deploys.workflow_name("github", "X") == "X" and deploys.workflow_name("gitlab", "X") is None


def test_failure_entries_and_the_publish_stage_rule():
    assert deploys.failure_entry({"name": "CI", "url": "u"}, {"name": "build"}, ["", "boom", "b", "c", "d"]) == {
        "step": "CI", "job": "build", "url": "u", "reasons": ["boom", "b", "c"]}
    assert deploys.failure_entry({}, {"url": "j"}, [])["reasons"] == [
        "no message was attached to this job; open it on the forge"]
    assert deploys.publish_stage_block("K-1", "regression") is None
    assert deploys.publish_stage_block("K-1", "in_dev") == "K-1 is in in_dev; it must pass QA first"


class Forge:
    name = "github"

    def __init__(self):
        self.run_list, self.job_list, self.prod, self.opened, self.merged = [], [], True, [], []
        self.pr = {"ok": True, "merge_sha": SHA}

    def pr_status(self, url): return self.pr
    def open_pr(self, repo, branch, title, body):
        self.opened.append((repo, branch, title)); return "https://h/p/1"
    def merge_pr(self, url): self.merged.append(url); return True
    def runs(self, repo, branch): return self.run_list
    def jobs(self, repo, run_id): return self.job_list
    def job_messages(self, repo, job_id): return ["it broke"]
    def dispatch_prod(self, repo, sha): return self.prod


@pytest.fixture
def board_parts():
    state = MemoryBoardState()
    state.add_ticket("K-1", "merged", summary="add search")
    commands = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 9.0)
    return state, commands


def test_the_observer_reads_the_merge_then_the_dev_deploy_then_prod(board_parts):
    state, commands = board_parts
    forge = Forge()
    obs = DeployObserverService(commands=commands, forge=forge, forge_for_url=lambda url: forge)
    assert obs.merge_sha("K-1") is None  # no PR recorded yet
    commands.record_note("K-1", "harness", "pr", "https://h/p/1")
    assert obs.merge_sha("K-1") == SHA and obs.latest("K-1", "merge:") == "merge:" + SHA

    assert obs.observe_dev("K-1", "o/r") == "waiting"
    forge.run_list = [{"sha": SHA, "name": "Build and Deploy", "status": "in_progress", "id": "1"}]
    assert obs.observe_dev("K-1", "o/r") == "running" and obs.dev_running("K-1") and not obs.dev_deployed("K-1")
    forge.run_list = [{"sha": SHA, "name": "Build and Deploy", "status": "completed", "conclusion": "success", "id": "1"}]
    assert obs.observe_dev("K-1", "o/r") == "success" and obs.dev_deployed("K-1")

    assert obs.start_prod_deploy("K-1", "o/r") is True and obs.latest("K-1", "prod:started:")
    forge.run_list = [{"sha": SHA, "name": "Deploy to prod", "status": "completed", "conclusion": "success", "id": "2"}]
    assert obs.observe_prod("K-1", "o/r") == "success" and obs.prod_deployed("K-1")


def test_a_failed_deploy_keeps_one_report_and_a_pass_clears_it(board_parts):
    state, commands = board_parts
    forge = Forge()
    obs = DeployObserverService(commands=commands, forge=forge, forge_for_url=lambda url: forge)
    commands.record_note("K-1", "observer", "observe", "merge:" + SHA)
    forge.run_list = [{"sha": SHA, "name": "Build and Deploy", "status": "completed", "conclusion": "failure",
                       "id": "1", "url": "run-url"}]
    forge.job_list = [{"id": "9", "name": "build", "conclusion": "failure", "url": "job-url"}]
    obs.observe_dev("K-1", "o/r")
    obs.observe_dev("K-1", "o/r")
    assert obs.failures("K-1") == [{"step": "Build and Deploy", "job": "build", "url": "job-url",
                                    "reasons": ["it broke"]}]
    assert len([e for e in state.history["K-1"] if e.kind == "failure"]) == 1
    forge.run_list[0]["conclusion"] = "success"
    obs.observe_dev("K-1", "o/r")
    assert obs.failures("K-1") == []


def test_prod_does_not_start_before_dev_succeeded_or_when_the_dispatch_fails(board_parts):
    state, commands = board_parts
    forge = Forge()
    obs = DeployObserverService(commands=commands, forge=forge, forge_for_url=lambda url: forge)
    assert obs.start_prod_deploy("K-1", "o/r") is False
    commands.record_note("K-1", "observer", "observe", "dev:success:" + SHA)
    forge.prod = False
    assert obs.start_prod_deploy("K-1", "o/r") is False
    assert obs.latest("K-1", "prod:") == "prod:start-failed:" + SHA


class Workspace:
    def __init__(self, remote="git@h:o/r.git", push=(True, "")):
        self.remote, self.push, self.pushed = remote, push, []

    def remote_url(self): return self.remote
    def push_branch(self, key): self.pushed.append(key); return self.push
    def branch_head(self, key): return "h" * 40
    def stray_commits(self, key): return True, []


class Prs:
    def status(self, url):
        return {"ok": True, "merged": False, "state": "OPEN", "checks": "passed", "commit_subjects": ["K-1: x"],
                "head": "h" * 40}


def publisher(state, commands, forge, workspace=None, hosted=True):
    ws = workspace or Workspace()
    bot = MergeBot(commands=commands, workspace=ws, pull_requests=Prs())
    return PublishService(commands=commands, queries=MemoryBoardQueries(state), workspace=ws,
                          target=PublishTarget(forge, "o/r", hosted), bot=bot,
                          merge_pr=lambda url: forge.merge_pr(url))


def test_a_pull_request_needs_qa_a_confirm_a_remote_and_a_push(board_parts):
    state, commands = board_parts
    forge = Forge()
    state.tickets["K-1"].stage = "in_dev"
    with pytest.raises(PublishBlocked, match="must pass QA first"):
        publisher(state, commands, forge).open_pull_request("K-1", confirm=True)
    state.tickets["K-1"].stage = "qa_approved"
    with pytest.raises(PublishBlocked, match="explicit confirm"):
        publisher(state, commands, forge).open_pull_request("K-1", confirm=False)
    with pytest.raises(PublishBlocked, match="no origin remote"):
        publisher(state, commands, forge, Workspace(remote=None)).open_pull_request("K-1", confirm=True)
    with pytest.raises(PublishBlocked, match="could not push agent/K-1: denied"):
        publisher(state, commands, forge, Workspace(push=(False, "denied"))).open_pull_request("K-1", confirm=True)
    with pytest.raises(PublishBlocked, match="no ticket K-9"):
        publisher(state, commands, forge).open_pull_request("K-9", confirm=True)
    assert forge.opened == []


def test_publishing_opens_the_pr_once_and_records_it(board_parts):
    state, commands = board_parts
    forge = Forge()
    state.tickets["K-1"].stage = "qa_approved"
    svc = publisher(state, commands, forge)
    assert svc.publish_ready() == ["K-1"]
    assert forge.opened == [("o/r", "agent/K-1", "K-1: add search")]
    assert commands.last_note("K-1", "pr") == "https://h/p/1"
    assert svc.publish_ready() == []  # it has a PR now
    assert publisher(state, commands, forge, hosted=False).publish_ready() == []


def test_a_pr_the_bot_approves_is_merged_and_the_ticket_moves_on(board_parts):
    state, commands = board_parts
    forge = Forge()
    state.tickets["K-1"].stage = "qa_approved"
    for role in ("pr_review", "perf_review", "qa"):
        commands.record_approval("K-1", role, "h" * 40)
    commands.record_note("K-1", "harness", "pr", "https://h/p/1")
    assert publisher(state, commands, forge).merge_when_clear() == ["K-1"]
    assert forge.merged == ["https://h/p/1"] and state.tickets["K-1"].stage == "merged"
    assert commands.last_note("K-1", "pr_merged") == "https://h/p/1"
