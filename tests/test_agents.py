import json
import os
import subprocess

import pytest

from tests.support import agents
from tests.support import board
from wf.adapters.runner import claude_code as claude_run
from tests.support import github_agent
from tests.support import merge
from tests.builders import PROJECT, clock, payload, sync, ticket, ticket_row

PROJECT_WITH_MERGED = {**PROJECT, "statuses": PROJECT["statuses"] + [{"name": "Merged to main", "category": "indeterminate"}]}

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "internal"
    r.mkdir()
    git(["init", "-q", "-b", "main"], r)
    (r / "app.txt").write_text("v1\n")
    git(["add", "-A"], r)
    git(["commit", "-q", "-m", "init"], r)
    return r


@pytest.fixture
def conn(tmp_path):
    c = board.connect(tmp_path / "wf.sqlite")
    agents.ensure_schema(c)
    sync(c, payload(projects=[PROJECT_WITH_MERGED], tickets=[ticket("ABC-1", status="Ready for dev")]))
    yield c
    c.close()


def fake_runner(calls):
    def run(argv, cwd, timeout):
        calls.append({"argv": argv, "cwd": cwd})
        return subprocess.CompletedProcess(argv, 0, stdout='{"type":"result"}\n', stderr="")
    return run


def run_dev(conn, repo, tmp_path, calls, role="dev", **kw):
    return agents.run_agent(conn, role, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", access=True,
                            model="m", claude_bin="/bin/echo", runner=fake_runner(calls), clock=clock, **kw)


# --- runs -----------------------------------------------------------------------------

def test_run_is_refused_without_claude_code_access(conn, repo, tmp_path):
    with pytest.raises(claude_run.ProviderNotAllowed):
        agents.run_agent(conn, "dev", "ABC-1", repo=repo, runs_dir=tmp_path / "runs", access=False,
                         model="m", claude_bin="/bin/echo", runner=fake_runner([]), clock=clock)


def test_dev_run_gets_its_own_branch_worktree_and_scoped_gateway(conn, repo, tmp_path):
    calls = []
    res = run_dev(conn, repo, tmp_path, calls, test_cmd="pytest -q")
    assert res.run.exit_code == 0
    wt = tmp_path / "runs" / "worktrees" / "ABC-1"
    assert wt.is_dir()
    assert git(["rev-parse", "--abbrev-ref", "HEAD"], wt) == "agent/ABC-1"
    argv = calls[0]["argv"]
    assert "--strict-mcp-config" in argv
    assert "Edit" in argv[argv.index("--tools") + 1:argv.index("--allowedTools")]
    cfg = json.loads((res.run.output_path.parent / "gateway.json").read_text())
    env = cfg["mcpServers"]["gateway"]["env"]
    assert env["WF_ROLE"] == "dev" and env["WF_KEY"] == "ABC-1" and env["WF_TEST_CMD"] == "pytest -q"


def test_read_only_roles_cannot_edit(conn, repo, tmp_path):
    calls = []
    run_dev(conn, repo, tmp_path, calls, role="pr_review")
    argv = calls[0]["argv"]
    builtin = argv[argv.index("--tools") + 1:argv.index("--allowedTools")]
    assert builtin == ["Read", "Glob", "Grep"]


def test_prompt_comes_from_the_role_file(conn, repo, tmp_path):
    calls = []
    run_dev(conn, repo, tmp_path, calls)
    system = calls[0]["argv"][calls[0]["argv"].index("--append-system-prompt") + 1]
    assert system.startswith("# Dev agent")


def test_tick_dry_run_lists_ready_work_and_runs_nothing(conn, repo, tmp_path):
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path / "runs", access=True, model="m", dry_run=True)
    assert actions == [{"key": "ABC-1", "stage": "ready_for_dev", "role": "dev", "skipped": None}]
    assert agents.runs_at(conn, "ABC-1", "ready_for_dev") == 0


def test_tick_skips_flagged_and_closed_sprint_tickets(conn, repo, tmp_path):
    board.set_flag(conn, "ABC-1", "needs-human", "user", "look", clock=clock)
    assert agents.tick(conn, repo=repo, runs_dir=tmp_path / "runs", access=True, model="m") == []


def test_run_limit_per_stage(conn, repo, tmp_path):
    for _ in range(agents.MAX_RUNS_PER_STAGE):
        conn.execute("insert into agent_run (key, stage, role, started_at, output_path) values ('ABC-1','ready_for_dev','dev',0,'x')")
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path / "runs", access=True, model="m")
    assert actions[0]["skipped"].startswith("run limit")


# --- merge gate --------------------------------------------------------------------

def _branch_with_change(repo, tmp_path):
    wt = agents.ensure_worktree(repo, "ABC-1", tmp_path / "runs" / "worktrees")
    (wt / "app.txt").write_text("v2\n")
    git(["add", "-A"], wt)
    git(["commit", "-q", "-m", "ABC-1: change"], wt)
    return wt


def _to_qa_approved(conn):
    conn.execute("update ticket set stage = 'qa_approved' where key = 'ABC-1'")
    conn.commit()


def test_merge_blocked_without_both_approvals(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _to_qa_approved(conn)
    head = agents.head_of(wt)
    board.record_approval(conn, "ABC-1", "qa", head, clock=clock)
    with pytest.raises(merge.MergeBlocked, match="pr_review"):
        merge.merge(conn, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", test_cmd="true")


def test_merge_blocked_on_an_old_head(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _to_qa_approved(conn)
    board.record_approval(conn, "ABC-1", "qa", agents.head_of(wt), clock=clock)
    board.record_approval(conn, "ABC-1", "pr_review", agents.head_of(wt), clock=clock)
    (wt / "app.txt").write_text("v3\n")
    git(["commit", "-qam", "more"], wt)  # new head, approvals are stale
    with pytest.raises(merge.MergeBlocked, match="missing approval"):
        merge.merge(conn, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", test_cmd="true")


def test_merge_blocked_when_tests_fail(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _to_qa_approved(conn)
    for role in ("qa", "pr_review", "perf_review"):
        board.record_approval(conn, "ABC-1", role, agents.head_of(wt), clock=clock)
    with pytest.raises(merge.MergeBlocked, match="tests fail"):
        merge.merge(conn, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", test_cmd="false")


def test_merge_succeeds_with_both_approvals_and_passing_tests(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _to_qa_approved(conn)
    head = agents.head_of(wt)
    for role in ("qa", "pr_review", "perf_review"):
        board.record_approval(conn, "ABC-1", role, head, clock=clock)
    merge.merge(conn, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", test_cmd="true", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "merged"
    assert (repo / "app.txt").read_text() == "v2\n"


# --- GitHub gate ----------------------------------------------------------------------

def test_github_refuses_before_the_pipeline_passes(conn, repo):
    with pytest.raises(github_agent.PublishBlocked, match="must pass QA first"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True)


def test_github_needs_confirm_and_a_configured_remote(conn, repo):
    conn.execute("update ticket set stage = 'merged' where key = 'ABC-1'")
    conn.commit()
    with pytest.raises(github_agent.PublishBlocked, match="confirm"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=False)
    with pytest.raises(github_agent.PublishBlocked, match="no origin remote"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True)


def test_github_pushes_the_branch_opens_the_pr_and_records_the_link(conn, repo):
    conn.execute("update ticket set stage = 'merged' where key = 'ABC-1'")
    conn.commit()
    git(["remote", "add", "origin", "git@example.invalid:o/r.git"], repo)
    seen = []

    def fake(argv, cwd):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="https://example.invalid/pr/1\n", stderr="")

    url = github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True, runner=fake)
    assert url == "https://example.invalid/pr/1"
    assert seen[0][:3] == ["git", "push", "-u"] and seen[1][:3] == ["gh", "pr", "create"]
    assert "Generated with [Claude Code]" in seen[1][-1]
    recorded = conn.execute("select detail from ticket_event where ticket_key='ABC-1' and kind='pr'").fetchone()[0]
    assert recorded == url


def test_diff_range_uses_the_merge_commit_once_merged(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    git(["checkout", "main"], repo)
    git(["merge", "--no-ff", "agent/ABC-1", "-m", "Merge agent/ABC-1 (ABC-1)"], repo)
    spec = agents.diff_range(repo, "ABC-1")
    assert spec.endswith("^1..." + spec.split("..")[-1]) or ".." in spec
    stat = git(["diff", "--stat", spec], repo)
    assert "app.txt" in stat


def test_pr_review_approval_moves_the_ticket_to_qa(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    git(["update-ref", "refs/remotes/origin/main", "main"], repo)  # GitHub's main, as last fetched
    conn.execute("update ticket set stage = 'pr_open' where key = 'ABC-1'")
    conn.commit()
    from wf.entrypoints import gateway as gateway_server
    g = gateway_server.Gateway(gateway_server.Scope({
        "WF_ROLE": "pr_review", "WF_KEY": "ABC-1", "WF_DB": str(conn.execute("pragma database_list").fetchone()[2]),
        "WF_WORKTREE": str(wt)}))
    g.approve_ticket({})
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_perf_review"
    assert "pr_review" in board.approvals_at(conn, "ABC-1", agents.head_of(wt))


def test_an_approval_at_head_moves_the_ticket_to_qa_without_another_review(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    conn.execute("update ticket set stage = 'pr_open' where key = 'ABC-1'")
    conn.commit()
    board.record_approval(conn, "ABC-1", "pr_review", agents.head_of(wt), clock=clock)
    moved = agents.advance_pipeline(conn, repo=repo)
    assert moved == ["ABC-1"]
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_perf_review"


def test_an_old_approval_does_not_move_a_ticket_after_new_commits(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    conn.execute("update ticket set stage = 'pr_open' where key = 'ABC-1'")
    conn.commit()
    board.record_approval(conn, "ABC-1", "pr_review", agents.head_of(wt), clock=clock)
    (wt / "app.txt").write_text("v9\n")
    git(["commit", "-qam", "more"], wt)
    assert agents.advance_pipeline(conn, repo=repo) == []


def _set_stage(conn, stage):
    conn.execute("update ticket set stage = ? where key = 'ABC-1'", (stage,))
    conn.commit()


def test_merged_goes_to_regression_only_after_the_github_merge(conn, repo, tmp_path):
    _branch_with_change(repo, tmp_path)
    _set_stage(conn, "merged")
    assert agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: False) == []
    assert ticket_row(conn, "ABC-1")["stage"] == "merged"
    assert agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: True) == ["ABC-1"]
    assert ticket_row(conn, "ABC-1")["stage"] == "regression"


def test_regression_pass_moves_to_deployed_on_dev(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _set_stage(conn, "regression")
    board.record_approval(conn, "ABC-1", "qa", agents.head_of(wt), clock=clock)
    agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: True)
    assert ticket_row(conn, "ABC-1")["stage"] == "deployed"


def test_regression_pass_goes_to_performance_when_the_dev_asked(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _set_stage(conn, "regression")
    board.request_perf_check(conn, "ABC-1", "dev", "changes a hot path")
    board.record_approval(conn, "ABC-1", "qa", agents.head_of(wt), clock=clock)
    agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: True)
    assert ticket_row(conn, "ABC-1")["stage"] == "performance"
    assert not agents.prod_ready(conn, "ABC-1", repo)  # performance still has to pass


def test_performance_pass_makes_the_ticket_ready_to_deploy(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _set_stage(conn, "performance")
    board.request_perf_check(conn, "ABC-1", "dev", "hot path")
    board.record_approval(conn, "ABC-1", "qa", agents.head_of(wt), clock=clock)
    board.record_approval(conn, "ABC-1", "performance", agents.head_of(wt), clock=clock)
    assert agents.prod_ready(conn, "ABC-1", repo)


def test_a_deploy_needs_a_regression_pass_on_the_current_commit(conn, repo, tmp_path):
    _branch_with_change(repo, tmp_path)
    _set_stage(conn, "regression")
    assert not agents.prod_ready(conn, "ABC-1", repo)


def test_only_the_dev_can_request_a_perf_check(conn):
    import pytest as _pt
    with _pt.raises(board.BoardError):
        board.request_perf_check(conn, "ABC-1", "qa", "x", clock=clock)


def test_an_approval_from_before_regression_does_not_count_as_a_regression_pass(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _set_stage(conn, "ready_for_qa")
    board.record_approval(conn, "ABC-1", "qa", agents.head_of(wt), clock=clock)
    board.move_stage(conn, "ABC-1", "regression", actor="user", reason="x")  # real time: after the approval
    agents.advance_pipeline(conn, repo=repo)
    assert ticket_row(conn, "ABC-1")["stage"] == "regression"


def test_regression_runs_against_the_dev_url_and_its_bot_signs_in_there(conn, repo, tmp_path):
    _set_stage(conn, "regression")
    calls = []
    res = agents.run_agent(conn, "qa", "ABC-1", repo=repo, runs_dir=tmp_path / "runs", access=True,
                           model="m", claude_bin="/bin/echo", runner=fake_runner(calls), clock=clock,
                           app_url="http://127.0.0.1:3002", regression_url="https://dev.example.com",
                           bot_domain="example.com", bot_credentials="/tmp/c.env")
    env = json.loads((res.run.output_path.parent / "gateway.json").read_text())["mcpServers"]["gateway"]["env"]
    assert env["WF_APP_URL"] == "https://dev.example.com"
    assert env["WF_BOT_APP"] == "https://dev.example.com"


def test_other_stages_keep_the_local_app(conn, repo, tmp_path):
    calls = []
    res = agents.run_agent(conn, "dev", "ABC-1", repo=repo, runs_dir=tmp_path / "runs", access=True,
                           model="m", claude_bin="/bin/echo", runner=fake_runner(calls), clock=clock,
                           app_url="http://127.0.0.1:3002", regression_url="https://dev.example.com")
    env = json.loads((res.run.output_path.parent / "gateway.json").read_text())["mcpServers"]["gateway"]["env"]
    assert env["WF_APP_URL"] == "http://127.0.0.1:3002"
    assert "WF_BOT_APP" not in env


def test_code_performance_review_approval_moves_the_ticket_to_qa(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    conn.execute("update ticket set stage = 'ready_for_perf_review' where key = 'ABC-1'")
    conn.commit()
    from wf.entrypoints import gateway as gateway_server
    g = gateway_server.Gateway(gateway_server.Scope({
        "WF_ROLE": "perf_review", "WF_KEY": "ABC-1", "WF_DB": str(conn.execute("pragma database_list").fetchone()[2]),
        "WF_WORKTREE": str(wt)}))
    g.approve_ticket({})
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_qa"
    assert "perf_review" in board.approvals_at(conn, "ABC-1", agents.head_of(wt))


def test_a_local_repo_merges_on_its_own_and_counts_as_dev(conn, repo, tmp_path):
    from tests.support import merge as merge_mod
    from tests.support import observe
    wt = _branch_with_change(repo, tmp_path)
    _set_stage(conn, "qa_approved")
    for role in ("qa", "pr_review", "perf_review"):
        board.record_approval(conn, "ABC-1", role, agents.head_of(wt), clock=clock)
    assert merge_mod.local_merge_when_clear(conn, repo=repo, runs_dir=tmp_path / "runs", test_cmd="true") == ["ABC-1"]
    assert ticket_row(conn, "ABC-1")["stage"] == "merged"
    assert observe.dev_deployed(conn, "ABC-1")
    assert (repo / "app.txt").read_text() == "v2\n"


def test_a_finished_stage_is_not_run_again_until_something_changes(conn, repo, tmp_path):
    _set_stage(conn, "regression")
    conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                 "values ('ABC-1','regression','qa',4000000000,0,0,'x')")
    conn.commit()
    assert agents._finished_and_unchanged(conn, "ABC-1", "regression")
    conn.execute("insert into ticket_event (ticket_key, at, actor, kind) values ('ABC-1', 4000000100, 'user', 'comment')")
    conn.commit()
    assert not agents._finished_and_unchanged(conn, "ABC-1", "regression")


def test_the_regression_brief_says_earlier_approvals_do_not_count(conn):
    _set_stage(conn, "regression")
    detail = board.ticket_detail(conn, "ABC-1")
    assert "Approvals from earlier stages do not count here" in agents.brief(detail)


def test_a_regression_pass_without_a_perf_request_goes_to_deployed(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _set_stage(conn, "regression")
    board.record_approval(conn, "ABC-1", "qa", agents.head_of(wt))
    agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: True)
    assert ticket_row(conn, "ABC-1")["stage"] == "deployed"


def test_a_sprint_backlog_ticket_goes_to_refinement(conn, repo, tmp_path):
    _set_stage(conn, "backlog")
    agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: False)
    assert ticket_row(conn, "ABC-1")["stage"] == "in_refinement"


def test_only_one_ticket_is_in_development_at_a_time(conn, repo, tmp_path):
    _set_stage(conn, "in_dev")  # ABC-1 is already being worked on
    conn.execute("insert into ticket (key, project_key, jira_id, summary, assignee_id, jira_status, labels, "
                 "sprint_id, stage, flags, removed_at, synced_at) values ('ABC-2','ABC','2','Two','me','Ready for dev','[]','101','ready_for_dev','[]',null,0)")
    conn.commit()
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path, access=False, model="m", dry_run=True)
    two = next(a for a in actions if a["key"] == "ABC-2")
    assert two["skipped"] == "dev is busy with ABC-1"



def test_a_review_that_sends_the_ticket_back_makes_the_stage_run_again(conn):
    _set_stage(conn, "in_dev")
    conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                 "values ('ABC-1','in_dev','dev',4000000000,0,0,'x')")
    conn.commit()
    assert agents._finished_and_unchanged(conn, "ABC-1", "in_dev")
    conn.execute("insert into ticket_event (ticket_key, at, actor, kind) values ('ABC-1', 4000000100, 'pr_review', 'stage')")
    conn.commit()
    assert not agents._finished_and_unchanged(conn, "ABC-1", "in_dev")


def test_a_dev_run_starts_from_the_latest_main(tmp_path):
    import subprocess as sp
    origin = tmp_path / "origin.git"
    sp.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    sp.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        sp.run(["git", *args], cwd=seed, check=True)
    (seed / "a.txt").write_text("one\n")
    sp.run(["git", "add", "-A"], cwd=seed, check=True)
    sp.run(["git", "commit", "-qm", "base"], cwd=seed, check=True)
    sp.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
    local = tmp_path / "local"
    sp.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    wt = agents.ensure_worktree(local, "ABC-9", tmp_path / "wt")
    (seed / "b.txt").write_text("two\n")
    sp.run(["git", "add", "-A"], cwd=seed, check=True)
    sp.run(["git", "commit", "-qm", "newer main"], cwd=seed, check=True)
    sp.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
    agents.sync_with_main(local, wt)
    assert (wt / "b.txt").exists()


def _refinement_run(conn, role):
    conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                 "values ('ABC-1', 'in_refinement', ?, 0, 0, 0, 'x')", (role,))
    conn.commit()


def test_refinement_takes_turns_product_then_staff(conn):
    agents.ensure_schema(conn)
    assert agents.refinement_role(conn, "ABC-1") == "product"
    _refinement_run(conn, "product")
    assert agents.refinement_role(conn, "ABC-1") == "staff"
    _refinement_run(conn, "staff")
    assert agents.refinement_role(conn, "ABC-1") == "product"


def test_removed_needs_refinement_stage_is_gone_from_the_workflow():
    from wf.domain.board import stages as mapping
    from tests.support import orchestrator
    assert "needs_refinement" not in mapping.STAGES
    assert "needs_refinement" not in orchestrator.STAGE_OWNERS
    assert "needs_refinement" not in agents.STAGE_ROLE


def _add_ticket(conn, key, stage, sort_order=None):
    conn.execute("insert into ticket (key, project_key, jira_id, summary, assignee_id, jira_status, labels, "
                 "sprint_id, stage, flags, removed_at, synced_at, sort_order) values (?, 'ABC', ?, 'x', 'me', "
                 "'Ready for dev', '[]', '101', ?, '[]', null, 0, ?)", (key, key, stage, sort_order))
    conn.commit()


def test_a_ticket_waits_for_its_prerequisite_to_merge_to_dev(conn, repo, tmp_path):
    from tests.support import board as _b
    _set_stage(conn, "deployed")  # the fixture's ticket is not in development
    _add_ticket(conn, "ABC-5", "ready_for_dev")
    _add_ticket(conn, "ABC-6", "pr_open")
    _b.add_block(conn, "ABC-6", "ABC-5", actor="staff", reason="needs the first")
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path, access=False, model="m", dry_run=True)
    row = next(a for a in actions if a["key"] == "ABC-5")
    assert row["skipped"] == "waiting for ABC-6 to merge to dev"


def test_a_kicked_back_prerequisite_pauses_the_work_that_depends_on_it(conn, repo, tmp_path):
    from tests.support import board as _b
    _add_ticket(conn, "ABC-7", "in_dev")       # the prerequisite, kicked back to dev
    _add_ticket(conn, "ABC-8", "pr_open")      # depends on it
    _b.add_block(conn, "ABC-7", "ABC-8", actor="staff", reason="needs the first")
    agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: False)
    assert ticket_row(conn, "ABC-8")["stage"] == "ready_for_dev"
    assert ticket_row(conn, "ABC-7")["stage"] == "in_dev"


def test_refinement_stops_after_seven_product_rounds_and_asks_a_person(conn, repo, tmp_path):
    _set_stage(conn, "in_refinement")
    for i in range(7):
        conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                     "values ('ABC-1','in_refinement','product',?,0,0,'x')", (1000 + i,))
    conn.commit()
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path, access=False, model="m", dry_run=False)
    row = next(a for a in actions if a["key"] == "ABC-1")
    assert "refinement rounds" in row["skipped"]
    assert "needs-human" in ticket_row(conn, "ABC-1")["flags"]


def test_the_dev_takes_the_sprint_order_first_and_unordered_last(conn, repo, tmp_path):
    _add_ticket(conn, "ABC-20", "ready_for_dev", sort_order=None)
    _add_ticket(conn, "ABC-21", "ready_for_dev", sort_order=2)
    _add_ticket(conn, "ABC-22", "ready_for_dev", sort_order=1)
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path, access=False, model="m", dry_run=True)
    picked = [a["key"] for a in actions if a["skipped"] is None]
    assert picked == ["ABC-22"]  # one dev at a time: the first in order


def test_pause_stops_a_pass_and_stage_moves_then_resume_restarts_them(conn, repo, tmp_path):
    from tests.support import pause
    _set_stage(conn, "backlog")
    pause.pause(conn, clock=lambda: 0)
    assert agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: False) == []
    assert ticket_row(conn, "ABC-1")["stage"] == "backlog"
    assert agents.tick(conn, repo=repo, runs_dir=tmp_path, access=False, model="m", dry_run=True) == []

    assert pause.resume(conn, clock=lambda: 1) is True
    agents.advance_pipeline(conn, repo=repo, dev_deployed=lambda k: False)
    assert ticket_row(conn, "ABC-1")["stage"] == "in_refinement"


def test_pause_is_idempotent_and_reports_state(conn):
    from tests.support import pause
    assert pause.is_paused(conn) is False
    assert pause.pause(conn, clock=lambda: 5) is True
    assert pause.pause(conn, clock=lambda: 6) is False
    assert pause.paused_since(conn) == 5
    assert pause.resume(conn, clock=lambda: 7) is True
    assert pause.resume(conn, clock=lambda: 8) is False
    assert pause.paused_since(conn) is None


def test_idle_reason_explains_a_finished_stage_and_offers_a_rerun(conn):
    _set_stage(conn, "in_dev")
    conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                 "values ('ABC-1', 'in_dev', 'dev', 4102444800, 0, 0, 'x')")  # after every event the fixture wrote
    conn.commit()
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is True
    assert "finished its last run" in reason["why"]


def test_idle_reason_says_what_blocks_a_ticket(conn):
    _set_stage(conn, "in_dev")
    conn.execute("update ticket set flags = '[\"needs-human\"]' where key = 'ABC-1'")
    conn.commit()
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is False and "needs-human" in reason["why"]

    conn.execute("update ticket set flags = '[]' where key = 'ABC-1'")
    conn.execute("update sprint set state = 'closed'")
    conn.commit()
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is False and "active sprint" in reason["why"]


def test_idle_reason_is_none_when_the_next_pass_will_pick_it_up(conn):
    _set_stage(conn, "ready_for_dev")
    assert agents.idle_reason(conn, "ABC-1") is None


def test_a_done_ticket_has_no_stall_notice(conn):
    _set_stage(conn, "deployed")
    assert agents.idle_reason(conn, "ABC-1") is None
