import json
import sqlite3

import pytest

from tests.builders import (  # noqa: F401
    NOW, PROJECT, STATUSES, backlog_ticket, clock, payload, sprint, sync, ticket, ticket_row,
)
from wf.adapters.persistence.sqlite.schema import SCHEMA_VERSION, connect
from wf.application.board.status import board_status
from wf.domain.board.errors import BoardError
from wf.entrypoints import cli


# --- schema -----------------------------------------------------------------------

def test_schema_version_and_foreign_keys(conn):
    assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into ticket (key, project_key, jira_id, summary, jira_status, synced_at) "
                     "values ('X-1', 'NOPE', '1', 's', 'st', 0)")


# --- first sync and the active board ---------------------------------------------

def test_first_sync_builds_the_active_board(conn, queries):
    r = sync(conn, payload(tickets=[ticket("ABC-1"), ticket("ABC-2", status="Dev in progress")]))
    assert r.counts["tickets_created"] == 2
    rows = {row["key"]: row for row in queries.active_board()}
    assert set(rows) == {"ABC-1", "ABC-2"}
    assert rows["ABC-1"]["stage"] == "ready_for_dev"
    assert rows["ABC-2"]["stage"] == "in_dev"
    assert rows["ABC-1"]["sprint_name"] == "Sprint 101"


def test_status_to_stage_comes_from_the_mapping(conn, queries):
    sync(conn, payload())
    assert queries.status_to_stage() == {
        "Backlog": "backlog",
        "Ready for dev": "ready_for_dev",
        "Dev in progress": "in_dev",
        "PR open": "pr_open",
        "Deployed to dev": "deployed",
    }


def test_unmapped_stages_are_reported_with_a_reason(conn, queries):
    sync(conn, payload())
    unmapped = {(r["project_key"], r["stage"]) for r in queries.unmapped_stages()}
    assert ("ABC", "in_refinement") in unmapped and ("ABC", "regression") in unmapped
    assert ("ABC", "ready_for_dev") not in unmapped


def test_sprint_state_changes_are_logged(conn, queries):
    sync(conn, payload())
    sync(conn, payload(sprints=[sprint("101", "closed")]))
    events = conn.execute("select from_state, to_state from sprint_event where sprint_id = '101' order by id").fetchall()
    assert [tuple(e) for e in events] == [(None, "active"), ("active", "closed")]
    assert [s["id"] for s in queries.sprints("closed")] == ["101"]


# --- removal and restore ------------------------------------------------------------

def test_ticket_missing_from_active_sync_is_removed_but_kept(conn, queries):
    sync(conn, payload(tickets=[ticket("ABC-1"), ticket("ABC-2")]))
    r = sync(conn, payload(tickets=[ticket("ABC-1")]))
    assert r.counts["removed"] == 1
    removed = ticket_row(conn, "ABC-2")
    assert removed is not None and removed["removed_at"] == int(NOW)
    assert {row["key"] for row in queries.active_board()} == {"ABC-1"}
    events = [e["detail"] for e in queries.ticket_detail("ABC-2")["events"]]
    assert "left the active sprint" in events


def test_ticket_whose_sprint_closed_is_removed(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    sync(conn, payload(sprints=[sprint("101", "closed")], tickets=[]))
    assert ticket_row(conn, "ABC-1")["removed_at"] is not None


def test_removed_ticket_that_returns_is_restored(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    sync(conn, payload(tickets=[]))
    r = sync(conn, payload(tickets=[ticket("ABC-1")]))
    assert r.counts["restored"] == 1
    assert ticket_row(conn, "ABC-1")["removed_at"] is None


# --- past sprints -----------------------------------------------------------------

def test_past_sync_fetches_closed_sprint_tickets(conn, queries):
    r = sync(conn, payload(kind="past", sprints=[sprint("100", "closed")],
                           tickets=[ticket("ABC-9", sprint_id="100", status="Deployed to dev")]))
    assert r.counts["tickets_created"] == 1
    assert ticket_row(conn, "ABC-9")["sprint_id"] == "100"
    assert queries.active_board() == []  # past tickets are not on the active board


def test_past_sync_never_overwrites_a_ticket_in_an_active_sprint(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    r = sync(conn, payload(kind="past", sprints=[sprint("100", "closed")],
                           tickets=[ticket("ABC-1", sprint_id="100", status="Deployed to dev")]))
    assert r.counts["skipped_active"] == 1
    row = ticket_row(conn, "ABC-1")
    assert row["jira_status"] == "Ready for dev" and row["sprint_id"] == "101"
    assert row["removed_at"] is None


def test_past_sync_does_not_remove_active_tickets(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    sync(conn, payload(kind="past", sprints=[sprint("100", "closed")], tickets=[]))
    assert ticket_row(conn, "ABC-1")["removed_at"] is None


# --- stage and status ----------------------------------------------------------------

def test_jira_status_change_moves_the_stage_and_is_logged(conn, queries):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    sync(conn, payload(tickets=[ticket("ABC-1", status="Dev in progress")]))
    row = ticket_row(conn, "ABC-1")
    assert row["stage"] == "in_dev" and row["jira_status"] == "Dev in progress"
    kinds = [(e["kind"], e["from_value"], e["to_value"]) for e in queries.ticket_detail("ABC-1")["events"]]
    assert ("status", "Ready for dev", "Dev in progress") in kinds
    assert ("stage", "ready_for_dev", "in_dev") in kinds


def test_local_stage_survives_a_sync_when_jira_status_is_unchanged(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    conn.execute("update ticket set stage = 'pr_open' where key = 'ABC-1'")
    conn.commit()
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    assert ticket_row(conn, "ABC-1")["stage"] == "pr_open"


# --- comments ---------------------------------------------------------------------

def test_jira_comments_are_upserted_and_local_comments_survive(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")],
                       comments=[{"ticket_key": "ABC-1", "jira_comment_id": "c1", "author": "Ana",
                                  "body": "first", "created": 5}]))
    conn.execute("insert into ticket_comment (ticket_key, origin, author, body, created_at) "
                 "values ('ABC-1', 'user', 'me', 'my note', 6)")
    conn.commit()
    sync(conn, payload(tickets=[ticket("ABC-1")],
                       comments=[{"ticket_key": "ABC-1", "jira_comment_id": "c1", "author": "Ana",
                                  "body": "edited", "created": 5, "updated": 7}]))
    rows = conn.execute("select origin, body from ticket_comment where ticket_key = 'ABC-1' order by id").fetchall()
    assert [tuple(r) for r in rows] == [("jira", "edited"), ("user", "my note")]


def test_comment_for_an_unknown_ticket_is_skipped(conn):
    r = sync(conn, payload(comments=[{"ticket_key": "ABC-404", "jira_comment_id": "c9",
                                      "author": "Ana", "body": "x", "created": 1}]))
    assert r.counts["comments_skipped_unknown_ticket"] == 1


# --- atomicity and validation -------------------------------------------------------

def test_failed_sync_changes_nothing_and_is_recorded(conn, queries):
    bad = payload(tickets=[ticket("ABC-1"), ticket("ZZZ-1", project_key="NOPE")])
    with pytest.raises(BoardError, match="rolled back"):
        sync(conn, bad)
    assert ticket_row(conn, "ABC-1") is None
    run = queries.last_sync()
    assert run["status"] == "failed" and run["error"]


def test_invalid_payload_is_refused_before_anything_is_written(conn):
    with pytest.raises(BoardError, match="kind"):
        sync(conn, payload(kind="weekly"))
    assert conn.execute("select count(*) from sync_run").fetchone()[0] == 0


def test_payload_shape_errors_name_the_field(conn):
    bad = payload(tickets=[{"key": "ABC-1", "project_key": "ABC", "jira_id": "1", "summary": "s"}])
    with pytest.raises(BoardError, match="'status'"):
        sync(conn, bad)


# --- reads and the CLI --------------------------------------------------------------

def test_ticket_detail_returns_comments_and_events(conn, queries):
    sync(conn, payload(tickets=[ticket("ABC-1")],
                       comments=[{"ticket_key": "ABC-1", "jira_comment_id": "c1", "author": "Ana",
                                  "body": "hi", "created": 1}]))
    detail = queries.ticket_detail("ABC-1")
    assert detail["ticket"]["summary"] == "Work ABC-1"
    assert [c["body"] for c in detail["comments"]] == ["hi"]
    assert detail["events"][0]["detail"] == "created"
    assert queries.ticket_detail("NOPE-1") is None


# --- backlog ------------------------------------------------------------------------

def test_backlog_sync_stores_tickets_in_no_sprint(conn, queries):
    r = sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-5")]))
    assert r.counts["tickets_created"] == 1
    assert ticket_row(conn, "ABC-5")["sprint_id"] is None
    assert [row["key"] for row in queries.backlog()] == ["ABC-5"]
    assert queries.active_board() == []  # the backlog is not the active sprint


def test_backlog_ticket_missing_from_backlog_sync_is_removed(conn, queries):
    sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-5"), backlog_ticket("ABC-6")]))
    r = sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-5")]))
    assert r.counts["removed"] == 1
    assert ticket_row(conn, "ABC-6")["removed_at"] is not None
    assert [row["key"] for row in queries.backlog()] == ["ABC-5"]


def test_backlog_sync_reflects_a_ticket_taken_out_of_its_sprint(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    r = sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-1", status="Backlog")]))
    assert r.counts.get("skipped_active", 0) == 0
    row = ticket_row(conn, "ABC-1")
    assert row["sprint_id"] is None and row["removed_at"] is None


def test_past_sync_still_does_not_overwrite_active_sprint_tickets(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    r = sync(conn, payload(kind="past", sprints=[sprint("100", "closed")],
                           tickets=[ticket("ABC-1", sprint_id="100", status="Deployed to dev")]))
    assert r.counts["skipped_active"] == 1
    assert ticket_row(conn, "ABC-1")["sprint_id"] == "101"


def test_ticket_moved_from_backlog_into_a_sprint_leaves_the_backlog(conn, queries):
    sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-7")]))
    sync(conn, payload(tickets=[ticket("ABC-7", status="Ready for dev")]))
    assert queries.backlog() == []
    assert [row["key"] for row in queries.active_board()] == ["ABC-7"]


def test_active_ticket_dropped_to_backlog_leaves_the_active_board(conn, queries):
    sync(conn, payload(tickets=[ticket("ABC-8")]))
    sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-8")]))
    assert queries.active_board() == []
    assert [row["key"] for row in queries.backlog()] == ["ABC-8"]


# --- harness moves, flags, and status ---------------------------------------------

def test_move_stage_to_a_mapped_stage_is_logged(conn, tickets, queries):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    tickets.move_stage("ABC-1", "pr_open", actor="dev", reason="opened PR")
    assert ticket_row(conn, "ABC-1")["stage"] == "pr_open"
    last = queries.ticket_detail("ABC-1")["events"][-1]
    assert (last["kind"], last["actor"], last["from_value"], last["to_value"]) == (
        "stage", "dev", "ready_for_dev", "pr_open")


def test_move_to_an_internal_only_stage_is_recorded_and_queues_nothing_for_jira(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    tickets.move_stage("ABC-1", "regression", actor="user", reason="x")
    assert ticket_row(conn, "ABC-1")["stage"] == "regression"
    assert conn.execute("select count(*) from outbox where ticket_key='ABC-1'").fetchone()[0] == 0


def test_move_rejects_unknown_stage_removed_ticket_and_missing_ticket(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    with pytest.raises(BoardError, match="unknown stage"):
        tickets.move_stage("ABC-1", "nowhere", actor="dev", reason="x")
    with pytest.raises(BoardError, match="no ticket"):
        tickets.move_stage("ABC-404", "pr_open", actor="dev", reason="x")
    sync(conn, payload(tickets=[]))
    with pytest.raises(BoardError, match="no longer in scope"):
        tickets.move_stage("ABC-1", "pr_open", actor="dev", reason="x")


def test_agents_can_flag_but_only_the_user_can_clear(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    tickets.set_flag("ABC-1", "needs-human", actor="dev", reason="scope unclear")
    assert json.loads(ticket_row(conn, "ABC-1")["flags"]) == ["needs-human"]
    with pytest.raises(BoardError, match="only the user"):
        tickets.clear_flag("ABC-1", "needs-human", actor="dev")
    tickets.clear_flag("ABC-1", "needs-human", actor="user")
    assert json.loads(ticket_row(conn, "ABC-1")["flags"]) == []


def test_flags_show_in_status(conn, tickets, queries):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    tickets.set_flag("ABC-1", "blocked", actor="qa", reason="env down")
    s = board_status(queries, NOW)
    assert [f["key"] for f in s["flagged"]] == ["ABC-1"]


def test_status_reports_age_and_blocked_stages(conn, queries):
    assert board_status(queries, NOW)["last_sync"] is None
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    s = board_status(queries, NOW + 3600)
    assert s["age_seconds"] == 3600 and s["active_tickets"] == 1
    assert any(u["stage"] == "regression" for u in s["unmapped_stages"])


# --- who may move what --------------------------------------------------------------

def _at(conn, key, stage):
    conn.execute("update ticket set stage = ? where key = ?", (stage, key))
    conn.commit()


WIDE_STATUSES = STATUSES + [
    {"name": "PR in review", "category": "indeterminate"},
    {"name": "Ready for QA", "category": "indeterminate"},
    {"name": "QA approved", "category": "indeterminate"},
    {"name": "Merged to main", "category": "indeterminate"},
]


def wide(**parts):
    """A payload whose project maps every stage the policy talks about."""
    return payload(projects=[{"key": "ABC", "name": "Example", "statuses": WIDE_STATUSES}], **parts)


def test_allowed_agent_moves_go_through(conn, tickets):
    sync(conn, wide(tickets=[ticket("ABC-1", status="Ready for dev")]))
    tickets.move_stage("ABC-1", "pr_open", actor="dev", reason="PR opened")
    _at(conn, "ABC-1", "pr_in_review")
    tickets.move_stage("ABC-1", "ready_for_qa", actor="harness", reason="approvals clear")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_qa"


def test_agent_move_outside_its_table_is_refused(conn, tickets):
    sync(conn, wide(tickets=[ticket("ABC-1", status="Ready for dev")]))
    _at(conn, "ABC-1", "ready_for_qa")
    with pytest.raises(BoardError, match="may not move"):
        tickets.move_stage("ABC-1", "qa_approved", actor="dev", reason="x")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_qa"


def test_skipping_review_is_refused_for_every_agent(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    for actor in ("dev", "qa", "staff", "product", "performance", "pr_review"):
        with pytest.raises(BoardError):
            tickets.move_stage("ABC-1", "merged", actor=actor, reason="x")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_product_and_performance_move_no_stages(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    for actor in ("product", "performance"):
        with pytest.raises(BoardError, match="may not move"):
            tickets.move_stage("ABC-1", "pr_open", actor=actor, reason="x")


def test_user_can_move_anywhere_mapped(conn, tickets):
    sync(conn, wide(tickets=[ticket("ABC-1", status="Ready for dev")]))
    tickets.move_stage("ABC-1", "pr_open", actor="user", reason="my call")
    tickets.move_stage("ABC-1", "merged", actor="user", reason="my call")
    assert ticket_row(conn, "ABC-1")["stage"] == "merged"


def test_unknown_actor_is_refused(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    with pytest.raises(BoardError, match="may not move"):
        tickets.move_stage("ABC-1", "pr_open", actor="some-bot", reason="x")


def test_workflow_statuses_survive_a_sync_without_tickets_in_them(conn, board_sync):
    sync(conn, payload(projects=[{**PROJECT, "statuses": STATUSES + [
        {"name": "Triage", "category": "new", "source": "workflow"}]}], tickets=[]))
    fresh = payload(projects=[{**PROJECT, "statuses": [{"name": "Ready for dev", "category": "new",
                                                       "source": "observed"}]}], tickets=[])
    board_sync.carry_workflow_statuses(fresh)
    sync(conn, fresh)
    names = {r["name"] for r in conn.execute("select name from project_status where project_key='ABC'")}
    assert "Triage" in names


def test_assignee_name_and_links_are_stored(conn):
    t = ticket("ABC-7", status="Ready for dev")
    t["assignee_name"] = "Pat Tester"
    t["links"] = [{"type": "blocks", "direction": "outward", "key": "ABC-8", "summary": "Next", "status": "To Do"}]
    sync(conn, payload(tickets=[t]))
    row = conn.execute("select assignee_name from ticket where key = 'ABC-7'").fetchone()
    assert row["assignee_name"] == "Pat Tester"
    link = conn.execute("select * from ticket_link where ticket_key = 'ABC-7'").fetchone()
    assert (link["other_key"], link["link_type"], link["other_summary"]) == ("ABC-8", "blocks", "Next")


def test_a_status_named_open_is_the_to_do_stage(conn):
    from wf.adapters.persistence.sqlite.board_sync import _stage_for
    assert _stage_for(conn, "ABC", "Open") == "backlog"
    assert _stage_for(conn, "ABC", "Backlog") == "backlog"
    assert _stage_for(conn, "ABC", "PR open") == "pr_open"


def test_connect_moves_tickets_from_the_removed_refinement_stage(tmp_path):
    p = tmp_path / "wf.sqlite"
    c = connect(p)
    sync(c, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    c.execute("update ticket set stage = 'needs_refinement' where key = 'ABC-1'")
    c.commit()
    c.close()
    again = connect(p)
    assert again.execute("select stage from ticket where key = 'ABC-1'").fetchone()[0] == "in_refinement"
    again.close()


def test_status_to_stage_ignores_mapping_rows_for_removed_stages(conn, queries):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    conn.execute("insert into project_status (project_key, name, category, source) "
                 "values ('ABC', 'Needs refinement', 'new', 'workflow')")
    conn.execute("insert into stage_mapping (project_key, stage, status_name, reason) "
                 "values ('ABC', 'needs_refinement', 'Needs refinement', 'old row')")
    conn.commit()
    assert "Needs refinement" not in queries.status_to_stage()


def test_a_block_is_stored_both_ways_and_waits_to_be_sent_to_jira(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1"), ticket("ABC-2")]))
    tickets.add_block("ABC-1", "ABC-2", actor="staff", reason="needs the button")
    assert tickets.blockers("ABC-2") == ["ABC-1"]
    assert tickets.pending_links() == [("ABC-1", "ABC-2")]
    tickets.mark_linked([("ABC-1", "ABC-2")])
    assert tickets.pending_links() == []


def test_a_ticket_cannot_block_itself(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    with pytest.raises(BoardError):
        tickets.add_block("ABC-1", "ABC-1", actor="staff", reason="x")


def test_set_order_records_the_position(conn, tickets):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    tickets.set_order("ABC-1", 3, actor="staff")
    assert conn.execute("select sort_order from ticket where key = 'ABC-1'").fetchone()[0] == 3
