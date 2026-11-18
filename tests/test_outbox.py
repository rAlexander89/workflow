import json

import pytest

from tests.support import board
from tests.support import outbox

from wf.entrypoints import cli

NOW = 1000.0


def clock():
    return NOW


STATUSES = [
    {"name": "Backlog", "category": "new"},
    {"name": "Ready for dev", "category": "new"},
    {"name": "Dev in progress", "category": "indeterminate"},
    {"name": "PR open", "category": "indeterminate"},
]


@pytest.fixture
def conn(tmp_path):
    c = board.connect(tmp_path / "wf.sqlite")
    board.apply_payload(c, {
        "kind": "active",
        "projects": [{"key": "ABC", "name": "Example", "statuses": STATUSES}],
        "sprints": [{"id": "101", "board_id": "7", "name": "Sprint 4", "state": "active"}],
        "tickets": [{"key": "ABC-1", "project_key": "ABC", "jira_id": "10001", "summary": "Add search",
                     "status": "Ready for dev", "status_category": "new", "labels": [], "sprint_id": "101"}],
    }, clock=clock)
    yield c
    c.close()


def pending_rows(conn, status="pending"):
    return conn.execute("select * from outbox where status = ? order by id", (status,)).fetchall()


def test_a_stage_move_is_queued_for_jira(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    rows = pending_rows(conn)
    assert len(rows) == 1
    assert (rows[0]["ticket_key"], rows[0]["target_stage"]) == ("ABC-1", "in_dev")


def test_a_newer_move_supersedes_the_older_pending_one(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    board.move_stage(conn, "ABC-1", "pr_open", actor="dev", reason="PR opened", clock=clock)
    assert [r["target_stage"] for r in pending_rows(conn)] == ["pr_open"]
    assert [r["target_stage"] for r in pending_rows(conn, "superseded")] == ["in_dev"]


def test_a_jira_side_status_change_is_not_queued_back(conn):
    board.apply_payload(conn, {
        "kind": "active",
        "tickets": [{"key": "ABC-1", "project_key": "ABC", "jira_id": "10001", "summary": "Add search",
                     "status": "Dev in progress", "status_category": "indeterminate", "labels": [],
                     "sprint_id": "101"}],
    }, clock=clock)
    assert pending_rows(conn) == [] and pending_rows(conn, "pending") == []


def test_plan_sends_the_transition_jira_allows(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    result = outbox.plan(conn, {"ABC-1": {"status": "Ready for dev",
                                          "transitions": [{"id": "31", "to": "Dev in progress"}]}}, clock=clock)
    assert [(s["key"], s["transition_id"], s["to_status"]) for s in result["send"]] == [
        ("ABC-1", "31", "Dev in progress")]
    assert pending_rows(conn, "sending")


def test_plan_never_sends_the_same_item_twice(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    obs = {"ABC-1": {"status": "Ready for dev", "transitions": [{"id": "31", "to": "Dev in progress"}]}}
    assert len(outbox.plan(conn, obs, clock=clock)["send"]) == 1
    assert outbox.plan(conn, obs, clock=clock)["send"] == []


def test_plan_skips_a_ticket_already_in_the_target_status(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    result = outbox.plan(conn, {"ABC-1": {"status": "Dev in progress", "transitions": []}}, clock=clock)
    assert result["send"] == [] and result["decided"] == {"skipped": 1}
    assert pending_rows(conn, "skipped")


def test_plan_does_not_force_a_move_when_jira_changed_since_last_sync(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    result = outbox.plan(conn, {"ABC-1": {"status": "PR open",
                                          "transitions": [{"id": "31", "to": "Dev in progress"}]}}, clock=clock)
    assert result["send"] == [] and result["decided"] == {"conflict": 1}
    assert pending_rows(conn, "conflict")
    assert "needs-human" in json.loads(board.ticket_detail(conn, "ABC-1")["ticket"]["flags"])


def test_plan_blocks_when_jira_has_no_such_transition(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    result = outbox.plan(conn, {"ABC-1": {"status": "Ready for dev", "transitions": []}}, clock=clock)
    assert result["send"] == [] and result["decided"] == {"blocked": 1}
    assert "no transition" in pending_rows(conn, "blocked")[0]["detail"]


def test_plan_asks_for_an_observation_it_does_not_have(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    result = outbox.plan(conn, {}, clock=clock)
    assert result["need"] == ["ABC-1"] and result["send"] == []
    assert pending_rows(conn)  # still waiting


def test_ack_marks_sent_and_updates_the_mirror(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    item = outbox.plan(conn, {"ABC-1": {"status": "Ready for dev",
                                        "transitions": [{"id": "31", "to": "Dev in progress"}]}},
                       clock=clock)["send"][0]
    counts = outbox.ack(conn, [{"id": item["id"], "ok": True, "jira_status": "Dev in progress",
                                "response": {"ok": True}}], clock=clock)
    assert counts == {"sent": 1, "failed": 0, "ignored": 0}
    assert pending_rows(conn, "sent")
    assert board.ticket_detail(conn, "ABC-1")["ticket"]["jira_status"] == "Dev in progress"
    # A second ack for the same item does nothing.
    assert outbox.ack(conn, [{"id": item["id"], "ok": True}], clock=clock)["ignored"] == 1


def test_failed_send_can_be_retried_but_a_sent_one_cannot(conn):
    board.move_stage(conn, "ABC-1", "in_dev", actor="user", reason="start", clock=clock)
    item = outbox.plan(conn, {"ABC-1": {"status": "Ready for dev",
                                        "transitions": [{"id": "31", "to": "Dev in progress"}]}},
                       clock=clock)["send"][0]
    outbox.ack(conn, [{"id": item["id"], "ok": False, "error": "HTTP 500"}], clock=clock)
    failed = pending_rows(conn, "failed")[0]
    assert failed["attempts"] == 1 and failed["detail"] == "HTTP 500"
    outbox.retry(conn, item["id"], clock=clock)
    assert pending_rows(conn)
    outbox.plan(conn, {"ABC-1": {"status": "Ready for dev",
                                 "transitions": [{"id": "31", "to": "Dev in progress"}]}}, clock=clock)
    outbox.ack(conn, [{"id": item["id"], "ok": True, "jira_status": "Dev in progress"}], clock=clock)
    with pytest.raises(board.BoardError, match="can be retried"):
        outbox.retry(conn, item["id"], clock=clock)


def test_cli_outbox_pending_and_plan(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps({
        "kind": "active",
        "projects": [{"key": "ABC", "name": "Example", "statuses": STATUSES}],
        "sprints": [{"id": "101", "board_id": "7", "name": "Sprint 4", "state": "active"}],
        "tickets": [{"key": "ABC-1", "project_key": "ABC", "jira_id": "10001", "summary": "Add search",
                     "status": "Ready for dev", "status_category": "new", "labels": [], "sprint_id": "101"}],
    }))
    cli.main(["sync", str(f), "--db", db])
    cli.main(["move", "ABC-1", "in_dev", "--actor", "user", "--reason", "start", "--db", db])
    capsys.readouterr()
    assert cli.main(["outbox", "pending", "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)[0]["ticket_key"] == "ABC-1"

    obs = tmp_path / "obs.json"
    obs.write_text(json.dumps({"observations": {"ABC-1": {
        "status": "Ready for dev", "transitions": [{"id": "31", "to": "Dev in progress"}]}}}))
    assert cli.main(["outbox", "plan", str(obs), "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["send"][0]["transition_id"] == "31"
