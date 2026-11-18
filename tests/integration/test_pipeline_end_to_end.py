"""The pipeline end to end over real SQLite and real git: agent runs, passes, advancing, merging, publishing.

Everything outside the machine is a fake: the `claude` process, `gh`, and the forge. The use cases come from the
container, the same way the CLI and the web app get them.
"""
import json
import subprocess
import sys

import pytest

from tests.builders import (
    GIT_ENV, PROJECT_WITH_MERGED, git, insert_ticket, make_container, payload, set_stage, sync, ticket, ticket_row,
)
from wf.adapters.runner.claude_code import ClaudeCodeRunner
from wf.application.delivery.publish import PublishBlocked
from wf.bootstrap import Overrides, Session
from wf.domain.board.errors import BoardError
from wf.domain.delivery import roles
from wf.domain.delivery.merge_gate import MergeBlocked
from wf.domain.execution import agent_run
from wf.domain.execution.model import ProviderNotAllowed
from wf.domain.execution.run_history import AgentRunRecord


def fake_claude(calls):
    """Stands in for the `claude` process."""
    def run(argv, cwd, timeout):
        calls.append({"argv": argv, "cwd": cwd})
        return subprocess.CompletedProcess(argv, 0, stdout='{"type":"result"}\n', stderr="")
    return run


class World:
    """A board with one ready ticket, a git repo, and the container's use cases over them."""

    def __init__(self, tmp_path, repo, conn, *, git_runner=None, forge=None, **config):
        sync(conn, payload(projects=[PROJECT_WITH_MERGED], tickets=[ticket("ABC-1", status="Ready for dev")]))
        self.conn, self.repo, self.tmp_path, self.calls = conn, repo, tmp_path, []
        runner = ClaudeCodeRunner(gateway_command=[sys.executable, "-m", "wf.entrypoints.gateway"],
                                  claude_bin="/bin/echo", runner=fake_claude(self.calls))
        self.container = make_container(tmp_path, repo=repo, overrides=Overrides(
            agent_runner=runner, git_runner=git_runner, forge=forge), **config)
        self.settings = self.container.settings()
        self.session = Session(self.container, conn)
        self.tickets = self.session.tickets
        self.workspace = self.session.workspace(self.settings)

    # use cases
    def pipeline(self):
        return self.session.pipeline(self.settings)

    def passes(self):
        return self.session.passes(self.settings)

    def merge(self):
        return self.session.merge(self.settings)

    def publish(self):
        return self.session.publish(self.settings)

    def observer(self):
        return self.session.observer(self.settings)

    def run_agent(self, role):
        return self.passes().run_agent(role, "ABC-1")

    # board helpers
    def stage(self, key="ABC-1"):
        return ticket_row(self.conn, key)["stage"]

    def set_stage(self, stage, key="ABC-1"):
        set_stage(self.conn, key, stage)

    def branch_with_change(self):
        wt = self.workspace.ensure_worktree("ABC-1")
        (wt / "app.txt").write_text("v2\n")
        git(["add", "-A"], wt)
        git(["commit", "-q", "-m", "ABC-1: change"], wt)
        return wt

    def head(self):
        return self.workspace.worktree_head("ABC-1")

    def approve(self, *roles_, head=None):
        for role in roles_:
            self.tickets.record_approval("ABC-1", role, head or self.head())

    def ran(self, stage, role, at, code=0):
        self.session.agent_runs.record(AgentRunRecord("ABC-1", stage, role, at, code, False, "x"))

    def dev_deployed(self):
        self.tickets.record_note("ABC-1", "observer", "observe", "dev:success:" + "a" * 40, once=True)


@pytest.fixture
def world(tmp_path, repo, conn):
    return World(tmp_path, repo, conn)


def env_of(result):
    cfg = json.loads((result.run.output_path.parent / "gateway.json").read_text())
    return cfg["mcpServers"]["gateway"]["env"]


# --- runs -----------------------------------------------------------------------------

def test_run_is_refused_without_claude_code_access(tmp_path, repo, conn):
    world = World(tmp_path, repo, conn, access=False)
    with pytest.raises(ProviderNotAllowed):
        world.run_agent("dev")


def test_dev_run_gets_its_own_branch_worktree_and_scoped_gateway(tmp_path, repo, conn):
    world = World(tmp_path, repo, conn, test_cmd="pytest -q")
    res = world.run_agent("dev")
    assert res.run.exit_code == 0
    wt = world.tmp_path / "runs" / "worktrees" / "ABC-1"
    assert wt.is_dir()
    assert git(["rev-parse", "--abbrev-ref", "HEAD"], wt) == "agent/ABC-1"
    argv = world.calls[0]["argv"]
    assert "--strict-mcp-config" in argv
    assert "Edit" in argv[argv.index("--tools") + 1:argv.index("--allowedTools")]
    env = env_of(res)
    assert env["WF_ROLE"] == "dev" and env["WF_KEY"] == "ABC-1" and env["WF_TEST_CMD"] == "pytest -q"


def test_read_only_roles_cannot_edit(world):
    world.run_agent("pr_review")
    argv = world.calls[0]["argv"]
    assert argv[argv.index("--tools") + 1:argv.index("--allowedTools")] == ["Read", "Glob", "Grep"]


def test_prompt_comes_from_the_role_file(world):
    world.run_agent("dev")
    system = world.calls[0]["argv"][world.calls[0]["argv"].index("--append-system-prompt") + 1]
    assert system.startswith("# Dev agent")


def test_regression_runs_against_the_dev_url_and_its_bot_signs_in_there(tmp_path, repo, conn):
    world = World(tmp_path, repo, conn, app_url="http://127.0.0.1:3002", regression_url="https://dev.example.com",
                  bots_domain="example.com", bots_credentials="/tmp/c.env")
    world.set_stage("regression")
    env = env_of(world.run_agent("qa"))
    assert env["WF_APP_URL"] == "https://dev.example.com"
    assert env["WF_BOT_APP"] == "https://dev.example.com"


def test_other_stages_keep_the_local_app(tmp_path, repo, conn):
    world = World(tmp_path, repo, conn, app_url="http://127.0.0.1:3002", regression_url="https://dev.example.com")
    env = env_of(world.run_agent("dev"))
    assert env["WF_APP_URL"] == "http://127.0.0.1:3002"
    assert "WF_BOT_APP" not in env


def test_the_regression_brief_says_earlier_approvals_do_not_count(world):
    world.set_stage("regression")
    detail = world.session.queries.ticket_detail("ABC-1")
    assert "Approvals from earlier stages do not count here" in agent_run.brief(detail)


def test_a_dev_run_starts_from_the_latest_main(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=seed, check=True)
    (seed / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    from wf.adapters.workspace.git import GitWorkspace
    workspace = GitWorkspace(local, worktrees=tmp_path / "wt")
    wt = workspace.ensure_worktree("ABC-9")
    (seed / "b.txt").write_text("two\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "newer main"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
    workspace.sync_with_main("ABC-9")
    assert (wt / "b.txt").exists()


# --- passes ---------------------------------------------------------------------------

def test_tick_dry_run_lists_ready_work_and_runs_nothing(world):
    assert world.passes().plan(dry_run=True) == [{"key": "ABC-1", "stage": "ready_for_dev", "role": "dev",
                                                  "skipped": None}]
    assert world.session.agent_runs.runs_for("ABC-1") == [] and world.calls == []


def test_tick_skips_flagged_and_closed_sprint_tickets(world):
    world.tickets.set_flag("ABC-1", "needs-human", "user", "look")
    assert world.passes().plan(dry_run=True) == []


def test_run_limit_per_stage(world):
    for i in range(roles.MAX_RUNS_PER_STAGE):
        world.ran("ready_for_dev", "dev", i, code=1)
    assert world.passes().plan(dry_run=True)[0]["skipped"].startswith("run limit")


def test_only_one_ticket_is_in_development_at_a_time(world):
    world.set_stage("in_dev")  # ABC-1 is already being worked on
    insert_ticket(world.conn, "ABC-2", "ready_for_dev")
    two = next(a for a in world.passes().plan(dry_run=True) if a["key"] == "ABC-2")
    assert two["skipped"] == "dev is busy with ABC-1"


def test_a_ticket_waits_for_its_prerequisite_to_merge_to_dev(world):
    world.set_stage("deployed")  # the fixture's ticket is not in development
    insert_ticket(world.conn, "ABC-5", "ready_for_dev")
    insert_ticket(world.conn, "ABC-6", "pr_open")
    world.tickets.add_block("ABC-6", "ABC-5", actor="staff", reason="needs the first")
    row = next(a for a in world.passes().plan(dry_run=True) if a["key"] == "ABC-5")
    assert row["skipped"] == "waiting for ABC-6 to merge to dev"


def test_the_dev_takes_the_sprint_order_first_and_unordered_last(world):
    insert_ticket(world.conn, "ABC-20", "ready_for_dev", sort_order=None)
    insert_ticket(world.conn, "ABC-21", "ready_for_dev", sort_order=2)
    insert_ticket(world.conn, "ABC-22", "ready_for_dev", sort_order=1)
    picked = [a["key"] for a in world.passes().plan(dry_run=True) if a["skipped"] is None]
    assert picked == ["ABC-22"]  # one dev at a time: the first in order


def test_refinement_takes_turns_product_then_staff(world):
    pipeline = world.pipeline()
    assert pipeline.refinement_role("ABC-1") == "product"
    world.ran("in_refinement", "product", 1)
    assert pipeline.refinement_role("ABC-1") == "staff"
    world.ran("in_refinement", "staff", 2)
    assert pipeline.refinement_role("ABC-1") == "product"


def test_refinement_stops_after_seven_product_rounds_and_asks_a_person(world):
    world.set_stage("in_refinement")
    for i in range(7):
        world.ran("in_refinement", "product", 1000 + i)
    row = next(a for a in world.passes().plan(dry_run=False) if a["key"] == "ABC-1")
    assert "refinement rounds" in row["skipped"]
    assert "needs-human" in ticket_row(world.conn, "ABC-1")["flags"]


def test_the_removed_needs_refinement_stage_is_gone_from_the_workflow():
    from wf.domain.board import stages
    from wf.domain.delivery.routing import STAGE_OWNERS
    assert "needs_refinement" not in stages.STAGES
    assert "needs_refinement" not in STAGE_OWNERS
    assert "needs_refinement" not in roles.STAGE_ROLE


def test_pause_stops_a_pass_and_stage_moves_then_resume_restarts_them(world):
    world.set_stage("backlog")
    world.session.pause.pause("test", 0)
    assert world.pipeline().advance() == []
    assert world.stage() == "backlog"
    assert world.passes().plan(dry_run=True) == []

    assert world.session.pause.resume(1) is True
    world.pipeline().advance()
    assert world.stage() == "in_refinement"


# --- idle reasons ----------------------------------------------------------------------

def test_a_finished_stage_is_not_run_again_until_something_changes(world):
    world.set_stage("regression")
    world.ran("regression", "qa", 4000000000)
    assert [a["key"] for a in world.passes().plan(dry_run=True)] == []  # it ran, and nothing happened since
    assert "finished its last run" in world.pipeline().idle_reason("ABC-1")["why"]
    world.conn.execute("insert into ticket_event (ticket_key, at, actor, kind) values ('ABC-1', 4000000100, 'user', 'comment')")
    world.conn.commit()
    assert [a["key"] for a in world.passes().plan(dry_run=True)] == ["ABC-1"]
    assert world.pipeline().idle_reason("ABC-1") is None


def test_a_review_that_sends_the_ticket_back_makes_the_stage_run_again(world):
    world.set_stage("in_dev")
    world.ran("in_dev", "dev", 4000000000)
    assert world.passes().plan(dry_run=True) == []
    world.conn.execute("insert into ticket_event (ticket_key, at, actor, kind) values ('ABC-1', 4000000100, 'pr_review', 'stage')")
    world.conn.commit()
    assert [a["key"] for a in world.passes().plan(dry_run=True)] == ["ABC-1"]


def test_idle_reason_says_what_blocks_a_ticket(world):
    world.set_stage("in_dev")
    world.tickets.set_flag("ABC-1", "needs-human", "qa", "unclear")
    reason = world.pipeline().idle_reason("ABC-1")
    assert reason["can_run"] is False and "needs-human" in reason["why"]
    world.tickets.clear_flag("ABC-1", "needs-human", "user")
    world.conn.execute("update sprint set state = 'closed'")
    world.conn.commit()
    reason = world.pipeline().idle_reason("ABC-1")
    assert reason["can_run"] is False and "active sprint" in reason["why"]


def test_idle_reason_is_none_when_the_next_pass_will_pick_it_up(world):
    assert world.pipeline().idle_reason("ABC-1") is None


def test_a_done_ticket_has_no_stall_notice(world):
    world.set_stage("deployed")
    assert world.pipeline().idle_reason("ABC-1") is None


# --- advancing --------------------------------------------------------------------------

def test_an_approval_at_head_moves_the_ticket_on_without_another_review(world):
    world.branch_with_change()
    world.set_stage("pr_open")
    world.approve("pr_review")
    assert world.pipeline().advance() == ["ABC-1"]
    assert world.stage() == "ready_for_perf_review"


def test_an_old_approval_does_not_move_a_ticket_after_new_commits(world):
    wt = world.branch_with_change()
    world.set_stage("pr_open")
    world.approve("pr_review")
    (wt / "app.txt").write_text("v9\n")
    git(["commit", "-qam", "more"], wt)
    assert world.pipeline().advance() == []


def test_a_flagged_ticket_stays_put_even_with_an_approval_at_head(world):
    world.branch_with_change()
    world.set_stage("pr_open")
    world.approve("pr_review")
    world.tickets.set_flag("ABC-1", "needs-human", "qa", "not approved after all")
    assert world.pipeline().advance() == [] and world.stage() == "pr_open"


def test_merged_goes_to_regression_only_after_the_dev_deploy(world):
    world.branch_with_change()
    world.set_stage("merged")
    assert world.pipeline().advance() == []
    assert world.stage() == "merged"
    world.dev_deployed()
    assert world.pipeline().advance() == ["ABC-1"]
    assert world.stage() == "regression"


def test_regression_pass_moves_to_deployed_on_dev(world):
    world.branch_with_change()
    world.set_stage("regression")
    world.approve("qa")
    world.pipeline().advance()
    assert world.stage() == "deployed"


def test_regression_pass_goes_to_performance_when_the_dev_asked(world):
    world.branch_with_change()
    world.set_stage("regression")
    world.tickets.request_perf_check("ABC-1", "dev", "changes a hot path")
    world.approve("qa")
    world.pipeline().advance()
    assert world.stage() == "performance"
    assert not world.session.deploy(world.settings).prod_ready("ABC-1")  # performance still has to pass


def test_performance_pass_makes_the_ticket_ready_to_deploy(world):
    world.branch_with_change()
    world.set_stage("performance")
    world.tickets.request_perf_check("ABC-1", "dev", "hot path")
    world.approve("qa", "performance")
    assert world.session.deploy(world.settings).prod_ready("ABC-1")


def test_a_deploy_needs_a_regression_pass_on_the_current_commit(world):
    world.branch_with_change()
    world.set_stage("regression")
    assert not world.session.deploy(world.settings).prod_ready("ABC-1")


def test_only_the_dev_can_request_a_perf_check(world):
    with pytest.raises(BoardError):
        world.tickets.request_perf_check("ABC-1", "qa", "x")


def test_an_approval_from_before_regression_does_not_count_as_a_regression_pass(tmp_path, repo, conn):
    class Ticking:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = Ticking()
    world = World(tmp_path, repo, conn, clock=clock)
    world.branch_with_change()
    world.set_stage("ready_for_qa")
    world.approve("qa")
    clock.now = 1001.0  # the ticket enters regression after the approval
    world.tickets.move_stage("ABC-1", "regression", actor="user", reason="x")
    world.pipeline().advance()
    assert world.stage() == "regression"


def test_a_sprint_backlog_ticket_goes_to_refinement(world):
    world.set_stage("backlog")
    world.pipeline().advance()
    assert world.stage() == "in_refinement"


def test_a_kicked_back_prerequisite_pauses_the_work_that_depends_on_it(world):
    insert_ticket(world.conn, "ABC-7", "in_dev")  # the prerequisite, kicked back to dev
    insert_ticket(world.conn, "ABC-8", "pr_open")  # depends on it
    world.tickets.add_block("ABC-7", "ABC-8", actor="staff", reason="needs the first")
    world.pipeline().advance()
    assert world.stage("ABC-8") == "ready_for_dev" and world.stage("ABC-7") == "in_dev"


# --- the merge gate ---------------------------------------------------------------------

def test_merge_blocked_without_all_approvals(world):
    world.branch_with_change()
    world.set_stage("qa_approved")
    world.approve("qa")
    with pytest.raises(MergeBlocked, match="pr_review"):
        world.merge().merge("ABC-1", "true")


def test_merge_blocked_on_an_old_head(world):
    wt = world.branch_with_change()
    world.set_stage("qa_approved")
    world.approve("qa", "pr_review")
    (wt / "app.txt").write_text("v3\n")
    git(["commit", "-qam", "more"], wt)  # new head, approvals are stale
    with pytest.raises(MergeBlocked, match="missing approval"):
        world.merge().merge("ABC-1", "true")


def test_merge_blocked_when_tests_fail(world):
    world.branch_with_change()
    world.set_stage("qa_approved")
    world.approve("qa", "pr_review", "perf_review")
    with pytest.raises(MergeBlocked, match="tests fail"):
        world.merge().merge("ABC-1", "false")


def test_merge_succeeds_with_all_approvals_and_passing_tests(world):
    world.branch_with_change()
    world.set_stage("qa_approved")
    world.approve("qa", "pr_review", "perf_review")
    world.merge().merge("ABC-1", "true")
    assert world.stage() == "merged"
    assert (world.repo / "app.txt").read_text() == "v2\n"


def test_a_local_repo_merges_on_its_own_and_counts_as_dev(world):
    world.branch_with_change()
    world.set_stage("qa_approved")
    world.approve("qa", "pr_review", "perf_review")
    assert world.merge().merge_local_when_clear(["ABC-1"], "true") == ["ABC-1"]
    assert world.stage() == "merged"
    assert world.observer().dev_deployed("ABC-1")
    assert (world.repo / "app.txt").read_text() == "v2\n"


def test_diff_range_uses_the_merge_commit_once_merged(world):
    world.branch_with_change()
    git(["checkout", "main"], world.repo)
    git(["merge", "--no-ff", "agent/ABC-1", "-m", "Merge agent/ABC-1 (ABC-1)"], world.repo)
    spec = world.workspace.diff_range("ABC-1")
    assert ".." in spec
    assert "app.txt" in git(["diff", "--stat", spec], world.repo)


# --- the gateway's approvals move tickets on ------------------------------------------------

def gateway_for(world, role, worktree):
    from wf.entrypoints import gateway
    return gateway.Gateway(gateway.Scope({"WF_ROLE": role, "WF_KEY": "ABC-1",
                                          "WF_DB": str(world.tmp_path / "wf.sqlite"), "WF_WORKTREE": str(worktree)}))


def test_pr_review_approval_moves_the_ticket_to_perf_review(world):
    wt = world.branch_with_change()
    git(["update-ref", "refs/remotes/origin/main", "main"], world.repo)  # GitHub's main, as last fetched
    world.set_stage("pr_open")
    gateway_for(world, "pr_review", wt).approve_ticket({})
    assert world.stage() == "ready_for_perf_review"
    assert "pr_review" in world.tickets.approvals_at("ABC-1", world.head())


def test_code_performance_review_approval_moves_the_ticket_to_qa(world):
    wt = world.branch_with_change()
    world.set_stage("ready_for_perf_review")
    gateway_for(world, "perf_review", wt).approve_ticket({})
    assert world.stage() == "ready_for_qa"
    assert "perf_review" in world.tickets.approvals_at("ABC-1", world.head())


# --- publishing a pull request -----------------------------------------------------------

def test_github_refuses_before_the_pipeline_passes(world):
    with pytest.raises(PublishBlocked, match="must pass QA first"):
        world.publish().open_pull_request("ABC-1", confirm=True)


def test_github_needs_confirm_and_a_configured_remote(world):
    world.set_stage("merged")
    with pytest.raises(PublishBlocked, match="confirm"):
        world.publish().open_pull_request("ABC-1", confirm=False)
    with pytest.raises(PublishBlocked, match="no origin remote"):
        world.publish().open_pull_request("ABC-1", confirm=True)


def test_github_pushes_the_branch_opens_the_pr_and_records_the_link(tmp_path, repo, conn):
    seen = []

    def fake(argv, cwd):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="https://example.invalid/pr/1\n", stderr="")

    world = World(tmp_path, repo, conn, git_runner=fake)
    world.set_stage("merged")
    git(["remote", "add", "origin", "git@example.invalid:o/r.git"], repo)
    url = world.publish().open_pull_request("ABC-1", confirm=True)
    assert url == "https://example.invalid/pr/1"
    assert seen[0][:3] == ["git", "push", "-u"] and seen[1][:3] == ["gh", "pr", "create"]
    assert "Generated with [Claude Code]" in seen[1][-1]
    assert world.tickets.last_note("ABC-1", "pr") == url
