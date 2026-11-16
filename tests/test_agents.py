import json
import os
import subprocess

import pytest

from wf import agents, board, claude_run, github_agent, merge
from tests.test_board import PROJECT, clock, payload, sync, ticket, ticket_row

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
    git(["commit", "-q", "-m", "change"], wt)
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
    for role in ("qa", "pr_review"):
        board.record_approval(conn, "ABC-1", role, agents.head_of(wt), clock=clock)
    with pytest.raises(merge.MergeBlocked, match="tests fail"):
        merge.merge(conn, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", test_cmd="false")


def test_merge_succeeds_with_both_approvals_and_passing_tests(conn, repo, tmp_path):
    wt = _branch_with_change(repo, tmp_path)
    _to_qa_approved(conn)
    head = agents.head_of(wt)
    for role in ("qa", "pr_review"):
        board.record_approval(conn, "ABC-1", role, head, clock=clock)
    merge.merge(conn, "ABC-1", repo=repo, runs_dir=tmp_path / "runs", test_cmd="true", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "merged"
    assert (repo / "app.txt").read_text() == "v2\n"


# --- GitHub gate ----------------------------------------------------------------------

def test_github_refuses_before_deployed(conn, repo):
    with pytest.raises(github_agent.PublishBlocked, match="must be deployed"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True)


def test_github_refuses_without_regression(conn, repo):
    conn.execute("update ticket set stage = 'deployed' where key = 'ABC-1'")
    conn.commit()
    with pytest.raises(github_agent.PublishBlocked, match="regression"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True)


def _deployed_after_regression(conn):
    conn.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value) "
                 "values ('ABC-1', 1, 'qa', 'stage', 'regression', 'deployed')")
    conn.execute("update ticket set stage = 'deployed' where key = 'ABC-1'")
    conn.commit()


def test_github_needs_confirm_and_a_configured_remote(conn, repo):
    _deployed_after_regression(conn)
    with pytest.raises(github_agent.PublishBlocked, match="confirm"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=False)
    with pytest.raises(github_agent.PublishBlocked, match="no origin remote"):
        github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True)


def test_github_opens_pr_only_with_all_conditions(conn, repo):
    _deployed_after_regression(conn)
    git(["remote", "add", "origin", "git@example.invalid:o/r.git"], repo)
    seen = []

    def fake_gh(argv, cwd):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="https://example.invalid/pr/1\n", stderr="")

    url = github_agent.open_pull_request(conn, "ABC-1", repo=repo, confirm=True, runner=fake_gh)
    assert url == "https://example.invalid/pr/1"
    assert seen[0][:3] == ["gh", "pr", "create"]
    assert "Generated with [Claude Code]" in seen[0][-1]


def test_tick_picks_up_ready_work_before_the_dev_run(conn, repo, tmp_path):
    calls = []
    actions = agents.tick(conn, repo=repo, runs_dir=tmp_path / "runs", access=True, model="m",
                          claude_bin="/bin/echo", runner=fake_runner(calls), dry_run=False, clock=clock)
    assert actions[0]["stage"] == "in_dev"
    assert ticket_row(conn, "ABC-1")["stage"] == "in_dev"
    assert len(calls) == 1


def test_dev_can_search_the_repo(conn, repo, tmp_path):
    calls = []
    run_dev(conn, repo, tmp_path, calls)
    argv = calls[0]["argv"]
    builtin = argv[argv.index("--tools") + 1:argv.index("--allowedTools")]
    assert {"Glob", "Grep", "Read", "Edit", "Write"} <= set(builtin)
