import json

import pytest

from tests.builders import STATUSES, payload, sync, ticket
from wf.domain.board.errors import BoardError
from wf.entrypoints import cli


@pytest.fixture
def conn(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    return conn


def pending_rows(conn, status="pending"):
    return conn.execute("select * from outbox where status = ? order by id", (status,)).fetchall()


def test_a_stage_move_is_queued_for_jira(conn, tickets):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    rows = pending_rows(conn)
    assert len(rows) == 1
    assert (rows[0]["ticket_key"], rows[0]["target_stage"]) == ("ABC-1", "in_dev")


def test_a_newer_move_supersedes_the_older_pending_one(conn, tickets):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    tickets.move_stage("ABC-1", "pr_open", actor="dev", reason="PR opened")
    assert [r["target_stage"] for r in pending_rows(conn)] == ["pr_open"]
    assert [r["target_stage"] for r in pending_rows(conn, "superseded")] == ["in_dev"]


def test_a_jira_side_status_change_is_not_queued_back(conn):
    sync(conn, {
        "kind": "active",
        "tickets": [{"key": "ABC-1", "project_key": "ABC", "jira_id": "10001", "summary": "Add search",
                     "status": "Dev in progress", "status_category": "indeterminate", "labels": [],
                     "sprint_id": "101"}],
    })
    assert pending_rows(conn) == [] and pending_rows(conn, "pending") == []


def test_plan_sends_the_transition_jira_allows(conn, tickets, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    result = outbox_service.plan({"ABC-1": {"status": "Ready for dev",
                                          "transitions": [{"id": "31", "to": "Dev in progress"}]}})
    assert [(s["key"], s["transition_id"], s["to_status"]) for s in result["send"]] == [
        ("ABC-1", "31", "Dev in progress")]
    assert pending_rows(conn, "sending")


def test_plan_never_sends_the_same_item_twice(conn, tickets, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    obs = {"ABC-1": {"status": "Ready for dev", "transitions": [{"id": "31", "to": "Dev in progress"}]}}
    assert len(outbox_service.plan(obs)["send"]) == 1
    assert outbox_service.plan(obs)["send"] == []


def test_plan_skips_a_ticket_already_in_the_target_status(conn, tickets, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    result = outbox_service.plan({"ABC-1": {"status": "Dev in progress", "transitions": []}})
    assert result["send"] == [] and result["decided"] == {"skipped": 1}
    assert pending_rows(conn, "skipped")


def test_plan_does_not_force_a_move_when_jira_changed_since_last_sync(conn, tickets, queries, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    result = outbox_service.plan({"ABC-1": {"status": "PR open",
                                          "transitions": [{"id": "31", "to": "Dev in progress"}]}})
    assert result["send"] == [] and result["decided"] == {"conflict": 1}
    assert pending_rows(conn, "conflict")
    assert "needs-human" in json.loads(queries.ticket_detail("ABC-1")["ticket"]["flags"])


def test_plan_blocks_when_jira_has_no_such_transition(conn, tickets, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    result = outbox_service.plan({"ABC-1": {"status": "Ready for dev", "transitions": []}})
    assert result["send"] == [] and result["decided"] == {"blocked": 1}
    assert "no transition" in pending_rows(conn, "blocked")[0]["detail"]


def test_plan_asks_for_an_observation_it_does_not_have(conn, tickets, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    result = outbox_service.plan({})
    assert result["need"] == ["ABC-1"] and result["send"] == []
    assert pending_rows(conn)  # still waiting


def test_ack_marks_sent_and_updates_the_mirror(conn, tickets, queries, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    item = outbox_service.plan({"ABC-1": {"status": "Ready for dev",
                                        "transitions": [{"id": "31", "to": "Dev in progress"}]}})["send"][0]
    counts = outbox_service.ack([{"id": item["id"], "ok": True, "jira_status": "Dev in progress",
                                "response": {"ok": True}}])
    assert counts == {"sent": 1, "failed": 0, "ignored": 0}
    assert pending_rows(conn, "sent")
    assert queries.ticket_detail("ABC-1")["ticket"]["jira_status"] == "Dev in progress"
    # A second ack for the same item does nothing.
    assert outbox_service.ack([{"id": item["id"], "ok": True}])["ignored"] == 1


def test_failed_send_can_be_retried_but_a_sent_one_cannot(conn, tickets, outbox_service):
    tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    item = outbox_service.plan({"ABC-1": {"status": "Ready for dev",
                                        "transitions": [{"id": "31", "to": "Dev in progress"}]}})["send"][0]
    outbox_service.ack([{"id": item["id"], "ok": False, "error": "HTTP 500"}])
    failed = pending_rows(conn, "failed")[0]
    assert failed["attempts"] == 1 and failed["detail"] == "HTTP 500"
    outbox_service.retry(item["id"])
    assert pending_rows(conn)
    outbox_service.plan({"ABC-1": {"status": "Ready for dev",
                                 "transitions": [{"id": "31", "to": "Dev in progress"}]}})
    outbox_service.ack([{"id": item["id"], "ok": True, "jira_status": "Dev in progress"}])
    with pytest.raises(BoardError, match="can be retried"):
        outbox_service.retry(item["id"])


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
