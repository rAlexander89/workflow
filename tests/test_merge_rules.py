"""The merge rules and services, with no git, GitHub, or database."""
from types import SimpleNamespace

import pytest

from tests.fakes import MemoryBoardState, MemoryBoardUnitOfWork
from wf.application.board.tickets import TicketCommands
from wf.application.delivery.merge import MergeBot, MergeService
from wf.domain.delivery import merge_gate
from wf.domain.delivery.merge_gate import MergeBlocked, PullRequestFacts, approval_block, stage_block, verdict

HEAD = "abcdef1234567890"
GOOD_PR = {"ok": True, "merged": False, "state": "OPEN", "checks": "passed", "commit_subjects": ["K-1: add it"],
           "head": HEAD}
ALL = frozenset({"pr_review", "perf_review", "qa"})


def test_the_internal_gate_rules():
    assert stage_block("K-1", "qa_approved") is None
    assert stage_block("K-1", "in_dev") == "K-1 is in in_dev, not qa_approved"
    assert approval_block("K-1", HEAD, set(ALL)) is None
    assert approval_block("K-1", HEAD, {"qa"}) == "K-1 at abcdef12 is missing approval from perf_review, pr_review"
    assert merge_gate.test_block(HEAD, "", None) == "no test command is configured, so the tests cannot be checked"
    assert merge_gate.test_block(HEAD, "make test", True) is None
    assert merge_gate.test_block(HEAD, "make test", False, "x" * 600) == "tests fail at abcdef12: " + "x" * 500


def facts(**kw):
    base = dict(key="K-1", pr_url="https://h/p/1", pr=GOOD_PR, branch_head=HEAD, approved_at_head=ALL,
                stray=(True, ()))
    return PullRequestFacts(**{**base, **kw})


def test_a_clean_pr_is_good_to_merge():
    assert verdict(facts()).ok


def test_each_reason_is_reported():
    assert verdict(PullRequestFacts("K-1", ticket_exists=False)).reasons == ["no ticket K-1"]
    assert verdict(PullRequestFacts("K-1", flagged=True)).reasons == [
        "the ticket is flagged; a person has to clear it", "there is no GitHub PR for this ticket yet"]
    assert "GitHub checks are still running" in verdict(facts(pr={**GOOD_PR, "checks": "pending"})).reasons
    assert "GitHub checks failed" in verdict(facts(pr={**GOOD_PR, "checks": "failed"})).reasons
    assert "GitHub reports no checks on this PR" in verdict(facts(pr={**GOOD_PR, "checks": "none"})).reasons
    assert "the PR is already merged" in verdict(facts(pr={**GOOD_PR, "merged": True})).reasons
    assert "the PR is closed" in verdict(facts(pr={**GOOD_PR, "state": "CLOSED"})).reasons
    assert verdict(facts(pr={"ok": False, "error": "boom"})).reasons[0] == "could not read the PR from GitHub: boom"
    assert any("not K-1" in r for r in verdict(facts(pr={**GOOD_PR, "commit_subjects": ["other work"]})).reasons)
    assert "the PR's head is not the branch's current commit; the PR needs updating first" in verdict(
        facts(pr={**GOOD_PR, "head": "0" * 16})).reasons
    assert verdict(facts(branch_head=None)).reasons == ["the ticket's branch is missing"]
    assert verdict(facts(approved_at_head=frozenset({"qa"}))).reasons == [
        "not approved on the current commit by: perf_review, pr_review"]
    assert verdict(facts(stray=(False, ("could not read the branch's commits",)))).reasons == [
        "could not read the branch's commits"]
    assert "the PR would also add 1 commit(s) that are not K-1, starting with: stuff" in verdict(
        facts(stray=(False, ("stuff",)))).reasons


# --- services with fake ports -------------------------------------------------------------------

class Workspace:
    def __init__(self, head=HEAD, worktree=True):
        self.head, self.worktree, self.merged, self.stray = head, worktree, [], (True, [])

    def branch_head(self, key): return self.head
    def worktree_exists(self, key): return self.worktree
    def worktree_head(self, key): return self.head
    def merge_branch(self, key): self.merged.append(key)
    def main_head(self): return "m" * 40
    def stray_commits(self, key): return self.stray


class Tests:
    __test__ = False

    def __init__(self, ok=True): self.ok, self.ran = ok, []

    def run(self, key, command):
        self.ran.append((key, command))
        return SimpleNamespace(ok=self.ok, output="boom")


@pytest.fixture
def parts():
    state = MemoryBoardState()
    state.add_ticket("K-1", "qa_approved")
    commands = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 50.0)
    for role in ("pr_review", "perf_review", "qa"):
        commands.record_approval("K-1", role, HEAD)
    return state, commands


def service(commands, workspace=None, tests=None):
    return MergeService(commands=commands, workspace=workspace or Workspace(), tests=tests or Tests())


def test_a_ticket_with_every_approval_and_passing_tests_merges(parts):
    state, commands = parts
    ws, tests = Workspace(), Tests()
    assert service(commands, ws, tests).merge("K-1", "make test") == HEAD
    assert ws.merged == ["K-1"] and tests.ran == [("K-1", "make test")]
    assert state.tickets["K-1"].stage == "merged"
    assert state.history["K-1"][-1].detail == "merged agent/K-1 at abcdef12"


def test_the_gate_blocks_for_each_reason(parts):
    state, commands = parts
    with pytest.raises(MergeBlocked, match="no ticket K-9"):
        service(commands).check("K-9", "t")
    with pytest.raises(MergeBlocked, match="no worktree for K-1"):
        service(commands, Workspace(worktree=False)).merge("K-1", "t")
    with pytest.raises(MergeBlocked, match="at 00000000 is missing approval"):
        service(commands, Workspace(head="0" * 40)).check("K-1", "t")
    with pytest.raises(MergeBlocked, match="no test command"):
        service(commands).check("K-1", "")
    with pytest.raises(MergeBlocked, match="tests fail at abcdef12: boom"):
        service(commands, tests=Tests(ok=False)).check("K-1", "t")
    state.tickets["K-1"].stage = "in_dev"
    with pytest.raises(MergeBlocked, match="is in in_dev, not qa_approved"):
        service(commands).check("K-1", "t")
    assert state.tickets["K-1"].stage == "in_dev"


def test_a_local_repo_merge_records_the_dev_deploy_once(parts):
    state, commands = parts
    state.add_ticket("K-2", "qa_approved")
    assert service(commands).merge_local_when_clear(["K-1", "K-2"], "t") == ["K-1"]  # K-2 has no approvals
    assert commands.notes("K-1", "observe") == ["dev:success:" + "m" * 40, "merge:" + "m" * 40]


class PullRequests:
    def __init__(self, status): self._status = status
    def status(self, url): return self._status


def test_the_merge_bot_reads_the_pr_and_the_branch(parts):
    state, commands = parts
    commands.record_note("K-1", "harness", "pr", "https://h/p/1")
    bot = MergeBot(commands=commands, workspace=Workspace(), pull_requests=PullRequests(GOOD_PR))
    assert bot.assess("K-1").ok
    state.tickets["K-1"].flags.append("blocked")
    assert bot.assess("K-1").reasons == ["the ticket is flagged; a person has to clear it"]
    assert bot.assess("K-9").reasons == ["no ticket K-9"]
    state.add_ticket("K-3", "qa_approved")
    assert bot.assess("K-3").reasons == ["there is no GitHub PR for this ticket yet"]
