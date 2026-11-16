import json
import subprocess

import pytest

from wf import board, jira_session, outbox
from wf.model import ProviderNotAllowed
from tests.test_board import clock, payload, sync, ticket


def fake(result_text, calls, returncode=0, is_error=False):
    def run(argv, cwd, timeout):
        calls.append(argv)
        out = json.dumps({"type": "result", "is_error": is_error, "result": result_text})
        return subprocess.CompletedProcess(argv, returncode, stdout=out, stderr="")
    return run


KW = dict(access=True, model="claude-sonnet-5-5", claude_bin="/bin/claude")


def test_session_is_refused_without_access(tmp_path):
    with pytest.raises(ProviderNotAllowed):
        jira_session.session("x", allowed=[], access=False, workdir=tmp_path, model="m", claude_bin="/bin/claude")


def test_session_uses_the_user_mcp_and_only_jira_read_tools(tmp_path):
    calls = []
    jira_session.session("x", allowed=jira_session.READ_TOOLS, workdir=tmp_path, runner=fake("{}", calls), **KW)
    argv = calls[0]
    assert "--strict-mcp-config" not in argv  # the user's connector loads in the session
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert all(t.startswith("mcp__atlassian__") for t in allowed)
    assert "transitionJiraIssue" not in " ".join(allowed)
    assert "Bash" in argv[argv.index("--disallowedTools") + 1:]


def test_sync_payload_is_applied_to_the_board(tmp_path):
    body = {"kind": "active", "projects": [{"key": "ABC", "name": "Example", "statuses": [
        {"name": "Ready for dev", "category": "new", "source": "observed"}]}],
        "sprints": [{"id": "3", "board_id": "1", "name": "Sprint 3", "state": "active"}],
        "tickets": [{"key": "ABC-1", "project_key": "ABC", "jira_id": "10001", "summary": "Fix",
                     "status": "Ready for dev", "status_category": "new", "sprint_id": "3", "updated": 1}],
        "comments": []}
    calls = []
    got = jira_session.fetch_sync_payload(workdir=tmp_path, runner=fake("```json\n" + json.dumps(body) + "\n```", calls), **KW)
    c = board.connect(tmp_path / "wf.sqlite")
    assert sync(c, got).counts.get("tickets_created") == 1


def test_sync_rejects_a_non_payload_answer(tmp_path):
    with pytest.raises(RuntimeError, match="active sync payload"):
        jira_session.fetch_sync_payload(workdir=tmp_path, runner=fake("[]", []), **KW)


def test_session_error_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match="reported an error"):
        jira_session.session("x", allowed=[], workdir=tmp_path, runner=fake("boom", [], is_error=True), **KW)


def test_send_goes_through_the_outbox_and_writes_only_what_was_planned(tmp_path):
    db = tmp_path / "wf.sqlite"
    c = board.connect(db)
    sync(c, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    board.move_stage(c, "ABC-1", "in_dev", "user", "test", clock=clock)
    observed = {"observations": {"ABC-1": {"status": "Ready for dev",
                                           "transitions": [{"id": "21", "to": "Dev in progress"}]}}}
    plan = outbox.plan(c, observed["observations"], clock=clock)
    sends = [{"id": s["id"], "key": s["key"], "transition_id": s["transition_id"], "to_status": s["to_status"]}
             for s in plan["send"]]
    assert sends and sends[0]["transition_id"] == "21"
    calls = []
    answer = [{"id": sends[0]["id"], "ok": True, "jira_status": "Dev in progress"}]
    results = jira_session.execute_sends(sends, workdir=tmp_path, runner=fake(json.dumps(answer), calls), **KW)
    assert "transitionJiraIssue" in " ".join(calls[0][calls[0].index("--allowedTools") + 1:calls[0].index("--disallowedTools")])
    outbox.ack(c, results, clock=clock)
    assert c.execute("select status from outbox").fetchone()[0] == "sent"
