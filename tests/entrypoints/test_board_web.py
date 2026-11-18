import json

import pytest
from fastapi.testclient import TestClient

from tests.builders import board_over
from wf.entrypoints.web.app import create_app

STATUSES = [
    {"name": "Backlog", "category": "new"},
    {"name": "Ready for dev", "category": "new"},
    {"name": "Dev in progress", "category": "indeterminate"},
]


def ticket(key, status="Ready for dev", sprint_id="101", **extra):
    t = {"key": key, "project_key": "ABC", "jira_id": "1" + key[-1], "summary": f"Work {key}",
         "status": status, "status_category": "new", "labels": [], "sprint_id": sprint_id}
    t.update(extra)
    return t


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "wf.sqlite"
    b = board_over(db)
    b.apply({
        "kind": "active",
        "projects": [{"key": "ABC", "name": "Example", "statuses": STATUSES}],
        "sprints": [{"id": "101", "board_id": "7", "name": "Sprint 4", "state": "active"}],
        "tickets": [ticket("ABC-1"), ticket("ABC-2", status="Dev in progress")],
        "comments": [{"ticket_key": "ABC-1", "jira_comment_id": "c1", "author": "Ana",
                      "body": "Keep the old endpoint.", "created": 1}],
    })
    b.apply({
        "kind": "backlog",
        "tickets": [ticket("ABC-9", status="Backlog", sprint_id=None)],
    })
    b.tickets.set_flag("ABC-1", "needs-human", actor="dev", reason="scope unclear")
    b.conn.close()
    app = create_app(config_path=tmp_path / "workflow.json", db_path=db)
    return TestClient(app, follow_redirects=False), db


def test_sprint_page_groups_tickets_by_stage(setup):
    client, _ = setup
    r = client.get("/board")
    assert r.status_code == 200
    assert "ready_for_dev" in r.text and "in_dev" in r.text
    assert "ABC-1" in r.text and "ABC-2" in r.text
    assert "ABC-9" not in r.text  # backlog tickets are not on the sprint page


def test_sprint_page_shows_sync_and_flags(setup):
    client, _ = setup
    r = client.get("/board")
    assert "Last sync: active ok" in r.text
    assert "needs-human" in r.text


def test_backlog_page_lists_only_unsprinted_tickets(setup):
    client, _ = setup
    r = client.get("/board/backlog")
    assert "ABC-9" in r.text and "ABC-1" not in r.text


def test_ticket_page_shows_comments_and_history(setup):
    client, _ = setup
    r = client.get("/board/ticket/ABC-1")
    assert r.status_code == 200
    assert "Work ABC-1" in r.text and "Keep the old endpoint." in r.text
    assert "sync update" in r.text  # sync events are hidden behind a count, not dropped


def test_unknown_ticket_is_404(setup):
    client, _ = setup
    assert client.get("/board/ticket/NOPE-1").status_code == 404


def test_user_clears_a_flag_from_the_ticket_page(setup):
    client, db = setup
    r = client.post("/board/ticket/ABC-1/flag/clear", data={"flag": "needs-human"})
    assert r.status_code == 303 and r.headers["location"] == "/board/ticket/ABC-1"
    with board_over(db) as b:
        assert json.loads(b.queries.ticket_detail("ABC-1")["ticket"]["flags"]) == []


def test_outbox_page_and_retry(setup):
    client, db = setup
    b = board_over(db)
    b.tickets.move_stage("ABC-1", "in_dev", actor="user", reason="start")
    item = b.outbox.plan({"ABC-1": {"status": "Ready for dev",
                                        "transitions": [{"id": "31", "to": "Dev in progress"}]}})["send"][0]
    b.outbox.ack([{"id": item["id"], "ok": False, "error": "HTTP 500"}])
    b.conn.close()

    r = client.get("/board/outbox")
    assert r.status_code == 200 and "ABC-1" in r.text and "HTTP 500" in r.text
    assert f"/board/outbox/{item['id']}/retry" in r.text

    r = client.post(f"/board/outbox/{item['id']}/retry")
    assert r.status_code == 303
    with board_over(db) as b:
        assert [o["status"] for o in b.outbox.listing()] == ["pending"]


def test_empty_board_says_never_synced(tmp_path):
    app = create_app(config_path=tmp_path / "workflow.json", db_path=tmp_path / "empty.sqlite")
    r = TestClient(app).get("/board")
    assert r.status_code == 200 and "Never synced" in r.text


def test_answer_records_the_human_reply_and_clears_the_flag(setup):
    client, db = setup
    with board_over(db) as b:
        b.tickets.set_flag("ABC-1", "needs-human", "dev", "which endpoint?")
    r = client.post("/board/ticket/ABC-1/answer", data={"answer": "Use /login, redirect is handled there."},
                    follow_redirects=False)
    assert r.status_code == 303
    c = board_over(db).conn
    body, origin = c.execute("select body, origin from ticket_comment where ticket_key='ABC-1' order by id desc limit 1").fetchone()
    assert body == "Use /login, redirect is handled there." and origin == "user"
    assert c.execute("select flags from ticket where key='ABC-1'").fetchone()[0] == "[]"


def test_send_back_moves_the_ticket_and_leaves_the_reason_for_the_agent(setup):
    client, db = setup
    with board_over(db) as b:
        b.tickets.move_stage("ABC-1", "in_dev", "user", "start")
        b.tickets.move_stage("ABC-1", "pr_open", "dev", "opened")
    r = client.post("/board/ticket/ABC-1/send-back", data={"to": "in_dev", "reason": "the redirect is lost"},
                    follow_redirects=False)
    assert r.status_code == 303
    c = board_over(db).conn
    assert c.execute("select stage from ticket where key='ABC-1'").fetchone()[0] == "in_dev"
    body = c.execute("select body from ticket_comment where ticket_key='ABC-1' order by id desc limit 1").fetchone()[0]
    assert "the redirect is lost" in body


def test_only_one_step_is_in_progress_at_a_time():
    from wf.entrypoints.web import pipeline as p
    for stage in ["in_dev", "pr_open", "pr_in_review", "ready_for_qa", "qa_approved", "merged", "regression", "performance", "deployed"]:
        for pr in (None, "https://x/pr/1"):
            flow = p.flow_states(stage, last_run_failed=False, perf_requested=True, pr_url=pr, pr_merged=False,
                                 pr_checks="pending", flagged=False)
            current = [f["key"] for f in flow if f["state"] == "current"]
            assert len(current) <= 1, (stage, pr, current)


def test_the_stage_marker_ends_at_done_and_prod_is_the_release():
    from wf.entrypoints.web import pipeline as p
    assert p.stage_marker("in_dev")[0] == "Stage: In Progress"
    assert p.stage_marker("regression")[0] == "Stage: Development"
    assert p.stage_marker("deployed")[0] == "Done"
    assert p.stage_marker("released")[0] == "Deployed to Prod"
