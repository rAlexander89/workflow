import json
import sqlite3

import pytest

from wf import board, cli

NOW = 1000.0


def clock():
    return NOW


STATUSES = [
    {"name": "Backlog", "category": "new"},
    {"name": "Ready for dev", "category": "new"},
    {"name": "Dev in progress", "category": "indeterminate"},
    {"name": "PR open", "category": "indeterminate"},
    {"name": "Deployed to dev", "category": "done"},
]
PROJECT = {"key": "ABC", "name": "Example", "statuses": STATUSES}


def sprint(sid, state, name=None):
    return {"id": sid, "board_id": "7", "name": name or f"Sprint {sid}", "state": state}


def ticket(key, status="Ready for dev", sprint_id="101", **extra):
    t = {"key": key, "project_key": "ABC", "jira_id": key.split("-")[1] + "00",
         "summary": f"Work {key}", "status": status, "status_category": "new",
         "labels": [], "sprint_id": sprint_id, "assignee_id": "me", "updated": 1}
    t.update(extra)
    return t


def payload(kind="active", **parts):
    data = {"kind": kind, "projects": [PROJECT], "sprints": [sprint("101", "active")],
            "tickets": [], "comments": []}
    data.update(parts)
    return data


@pytest.fixture
def conn(tmp_path):
    c = board.connect(tmp_path / "wf.sqlite")
    yield c
    c.close()


def sync(conn, data):
    return board.apply_payload(conn, data, clock=clock)


def ticket_row(conn, key):
    return conn.execute("select * from ticket where key = ?", (key,)).fetchone()


# --- schema -----------------------------------------------------------------------

def test_schema_version_and_foreign_keys(conn):
    assert conn.execute("pragma user_version").fetchone()[0] == board.SCHEMA_VERSION
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into ticket (key, project_key, jira_id, summary, jira_status, synced_at) "
                     "values ('X-1', 'NOPE', '1', 's', 'st', 0)")


# --- first sync and the active board ---------------------------------------------

def test_first_sync_builds_the_active_board(conn):
    r = sync(conn, payload(tickets=[ticket("ABC-1"), ticket("ABC-2", status="Dev in progress")]))
    assert r.counts["tickets_created"] == 2
    rows = {row["key"]: row for row in board.active_board(conn)}
    assert set(rows) == {"ABC-1", "ABC-2"}
    assert rows["ABC-1"]["stage"] == "ready_for_dev"
    assert rows["ABC-2"]["stage"] == "in_dev"
    assert rows["ABC-1"]["sprint_name"] == "Sprint 101"


def test_status_to_stage_comes_from_the_mapping(conn):
    sync(conn, payload())
    assert board.status_to_stage(conn) == {
        "Backlog": "backlog",
        "Ready for dev": "ready_for_dev",
        "Dev in progress": "in_dev",
        "PR open": "pr_open",
        "Deployed to dev": "deployed",
    }


def test_unmapped_stages_are_reported_with_a_reason(conn):
    sync(conn, payload())
    unmapped = {(r["project_key"], r["stage"]) for r in board.unmapped_stages(conn)}
    assert ("ABC", "in_refinement") in unmapped and ("ABC", "regression") in unmapped
    assert ("ABC", "ready_for_dev") not in unmapped


def test_sprint_state_changes_are_logged(conn):
    sync(conn, payload())
    sync(conn, payload(sprints=[sprint("101", "closed")]))
    events = conn.execute("select from_state, to_state from sprint_event where sprint_id = '101' order by id").fetchall()
    assert [tuple(e) for e in events] == [(None, "active"), ("active", "closed")]
    assert [s["id"] for s in board.sprints(conn, "closed")] == ["101"]


# --- removal and restore ------------------------------------------------------------

def test_ticket_missing_from_active_sync_is_removed_but_kept(conn):
    sync(conn, payload(tickets=[ticket("ABC-1"), ticket("ABC-2")]))
    r = sync(conn, payload(tickets=[ticket("ABC-1")]))
    assert r.counts["removed"] == 1
    removed = ticket_row(conn, "ABC-2")
    assert removed is not None and removed["removed_at"] == int(NOW)
    assert {row["key"] for row in board.active_board(conn)} == {"ABC-1"}
    events = [e["detail"] for e in board.ticket_detail(conn, "ABC-2")["events"]]
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

def test_past_sync_fetches_closed_sprint_tickets(conn):
    r = sync(conn, payload(kind="past", sprints=[sprint("100", "closed")],
                           tickets=[ticket("ABC-9", sprint_id="100", status="Deployed to dev")]))
    assert r.counts["tickets_created"] == 1
    assert ticket_row(conn, "ABC-9")["sprint_id"] == "100"
    assert board.active_board(conn) == []  # past tickets are not on the active board


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

def test_jira_status_change_moves_the_stage_and_is_logged(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    sync(conn, payload(tickets=[ticket("ABC-1", status="Dev in progress")]))
    row = ticket_row(conn, "ABC-1")
    assert row["stage"] == "in_dev" and row["jira_status"] == "Dev in progress"
    kinds = [(e["kind"], e["from_value"], e["to_value"]) for e in board.ticket_detail(conn, "ABC-1")["events"]]
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

def test_failed_sync_changes_nothing_and_is_recorded(conn):
    bad = payload(tickets=[ticket("ABC-1"), ticket("ZZZ-1", project_key="NOPE")])
    with pytest.raises(board.BoardError, match="rolled back"):
        sync(conn, bad)
    assert ticket_row(conn, "ABC-1") is None
    run = board.last_sync(conn)
    assert run["status"] == "failed" and run["error"]


def test_invalid_payload_is_refused_before_anything_is_written(conn):
    with pytest.raises(board.BoardError, match="kind"):
        sync(conn, payload(kind="weekly"))
    assert conn.execute("select count(*) from sync_run").fetchone()[0] == 0


def test_payload_shape_errors_name_the_field(conn):
    bad = payload(tickets=[{"key": "ABC-1", "project_key": "ABC", "jira_id": "1", "summary": "s"}])
    with pytest.raises(board.BoardError, match="'status'"):
        sync(conn, bad)


# --- reads and the CLI --------------------------------------------------------------

def test_ticket_detail_returns_comments_and_events(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")],
                       comments=[{"ticket_key": "ABC-1", "jira_comment_id": "c1", "author": "Ana",
                                  "body": "hi", "created": 1}]))
    detail = board.ticket_detail(conn, "ABC-1")
    assert detail["ticket"]["summary"] == "Work ABC-1"
    assert [c["body"] for c in detail["comments"]] == ["hi"]
    assert detail["events"][0]["detail"] == "created"
    assert board.ticket_detail(conn, "NOPE-1") is None


def test_cli_sync_board_and_sprints(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps(payload(tickets=[ticket("ABC-1")])))
    assert cli.main(["sync", str(f), "--db", db]) == 0
    assert "tickets_created=1" in capsys.readouterr().out

    assert cli.main(["board", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "ready_for_dev" in out and "ABC-1" in out

    assert cli.main(["sprints", "--state", "active", "--db", db]) == 0
    assert "Sprint 101" in capsys.readouterr().out


def test_cli_sync_reports_a_bad_file(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    assert cli.main(["sync", str(f), "--db", db]) == 2
    assert "could not read" in capsys.readouterr().err


# --- backlog ------------------------------------------------------------------------

def backlog_ticket(key, status="Backlog", **extra):
    return ticket(key, status=status, sprint_id=None, **extra)


def test_backlog_sync_stores_tickets_in_no_sprint(conn):
    r = sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-5")]))
    assert r.counts["tickets_created"] == 1
    assert ticket_row(conn, "ABC-5")["sprint_id"] is None
    assert [row["key"] for row in board.backlog(conn)] == ["ABC-5"]
    assert board.active_board(conn) == []  # the backlog is not the active sprint


def test_backlog_ticket_missing_from_backlog_sync_is_removed(conn):
    sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-5"), backlog_ticket("ABC-6")]))
    r = sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-5")]))
    assert r.counts["removed"] == 1
    assert ticket_row(conn, "ABC-6")["removed_at"] is not None
    assert [row["key"] for row in board.backlog(conn)] == ["ABC-5"]


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


def test_ticket_moved_from_backlog_into_a_sprint_leaves_the_backlog(conn):
    sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-7")]))
    sync(conn, payload(tickets=[ticket("ABC-7", status="Ready for dev")]))
    assert board.backlog(conn) == []
    assert [row["key"] for row in board.active_board(conn)] == ["ABC-7"]


def test_active_ticket_dropped_to_backlog_leaves_the_active_board(conn):
    sync(conn, payload(tickets=[ticket("ABC-8")]))
    sync(conn, payload(kind="backlog", tickets=[backlog_ticket("ABC-8")]))
    assert board.active_board(conn) == []
    assert [row["key"] for row in board.backlog(conn)] == ["ABC-8"]


def test_cli_backlog_lists_tickets(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "backlog.json"
    f.write_text(json.dumps(payload(kind="backlog", tickets=[backlog_ticket("ABC-5")])))
    assert cli.main(["sync", str(f), "--db", db]) == 0
    capsys.readouterr()
    assert cli.main(["backlog", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "ABC-5" in out and "ready_for_dev" not in out


# --- harness moves, flags, and status ---------------------------------------------

def test_move_stage_to_a_mapped_stage_is_logged(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    board.move_stage(conn, "ABC-1", "pr_open", actor="dev", reason="opened PR", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "pr_open"
    last = board.ticket_detail(conn, "ABC-1")["events"][-1]
    assert (last["kind"], last["actor"], last["from_value"], last["to_value"]) == (
        "stage", "dev", "ready_for_dev", "pr_open")


def test_move_to_an_internal_only_stage_is_recorded_and_queues_nothing_for_jira(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    board.move_stage(conn, "ABC-1", "regression", actor="user", reason="x", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "regression"
    assert conn.execute("select count(*) from outbox where ticket_key='ABC-1'").fetchone()[0] == 0


def test_move_rejects_unknown_stage_removed_ticket_and_missing_ticket(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    with pytest.raises(board.BoardError, match="unknown stage"):
        board.move_stage(conn, "ABC-1", "nowhere", actor="dev", reason="x", clock=clock)
    with pytest.raises(board.BoardError, match="no ticket"):
        board.move_stage(conn, "ABC-404", "pr_open", actor="dev", reason="x", clock=clock)
    sync(conn, payload(tickets=[]))
    with pytest.raises(board.BoardError, match="no longer in scope"):
        board.move_stage(conn, "ABC-1", "pr_open", actor="dev", reason="x", clock=clock)


def test_agents_can_flag_but_only_the_user_can_clear(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    board.set_flag(conn, "ABC-1", "needs-human", actor="dev", reason="scope unclear", clock=clock)
    assert json.loads(ticket_row(conn, "ABC-1")["flags"]) == ["needs-human"]
    with pytest.raises(board.BoardError, match="only the user"):
        board.clear_flag(conn, "ABC-1", "needs-human", actor="dev", clock=clock)
    board.clear_flag(conn, "ABC-1", "needs-human", actor="user", clock=clock)
    assert json.loads(ticket_row(conn, "ABC-1")["flags"]) == []


def test_flags_show_in_status(conn):
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    board.set_flag(conn, "ABC-1", "blocked", actor="qa", reason="env down", clock=clock)
    s = board.status(conn, NOW)
    assert [f["key"] for f in s["flagged"]] == ["ABC-1"]


def test_status_reports_age_and_blocked_stages(conn):
    assert board.status(conn, NOW)["last_sync"] is None
    sync(conn, payload(tickets=[ticket("ABC-1")]))
    s = board.status(conn, NOW + 3600)
    assert s["age_seconds"] == 3600 and s["active_tickets"] == 1
    assert any(u["stage"] == "regression" for u in s["unmapped_stages"])


def test_cli_status_flags_a_stale_board(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps(payload(tickets=[ticket("ABC-1")])))
    cli.main(["sync", str(f), "--db", db])
    capsys.readouterr()
    cli.main(["status", "--db", db])  # real clock: the sync just happened
    assert "stale" not in capsys.readouterr().out


def test_cli_move_and_flag(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps(payload(tickets=[ticket("ABC-1", status="Ready for dev")])))
    cli.main(["sync", str(f), "--db", db])
    capsys.readouterr()
    assert cli.main(["move", "ABC-1", "pr_open", "--actor", "dev", "--reason", "opened PR", "--db", db]) == 0
    assert cli.main(["move", "ABC-1", "regression", "--actor", "qa", "--reason", "x", "--db", db]) == 2
    assert cli.main(["flag", "ABC-1", "needs-human", "--actor", "dev", "--reason", "unclear", "--db", db]) == 0
    assert cli.main(["flag", "ABC-1", "needs-human", "--actor", "dev", "--clear", "--db", db]) == 2
    assert cli.main(["flag", "ABC-1", "needs-human", "--actor", "user", "--clear", "--db", db]) == 0
    assert "error" in capsys.readouterr().err


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


def test_allowed_agent_moves_go_through(conn):
    sync(conn, wide(tickets=[ticket("ABC-1", status="Ready for dev")]))
    board.move_stage(conn, "ABC-1", "pr_open", actor="dev", reason="PR opened", clock=clock)
    _at(conn, "ABC-1", "pr_in_review")
    board.move_stage(conn, "ABC-1", "ready_for_qa", actor="harness", reason="approvals clear", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_qa"


def test_agent_move_outside_its_table_is_refused(conn):
    sync(conn, wide(tickets=[ticket("ABC-1", status="Ready for dev")]))
    _at(conn, "ABC-1", "ready_for_qa")
    with pytest.raises(board.BoardError, match="may not move"):
        board.move_stage(conn, "ABC-1", "qa_approved", actor="dev", reason="x", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_qa"


def test_skipping_review_is_refused_for_every_agent(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    for actor in ("dev", "qa", "staff", "product", "performance", "pr_review"):
        with pytest.raises(board.BoardError):
            board.move_stage(conn, "ABC-1", "merged", actor=actor, reason="x", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_product_and_performance_move_no_stages(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    for actor in ("product", "performance"):
        with pytest.raises(board.BoardError, match="may not move"):
            board.move_stage(conn, "ABC-1", "pr_open", actor=actor, reason="x", clock=clock)


def test_user_can_move_anywhere_mapped(conn):
    sync(conn, wide(tickets=[ticket("ABC-1", status="Ready for dev")]))
    board.move_stage(conn, "ABC-1", "pr_open", actor="user", reason="my call", clock=clock)
    board.move_stage(conn, "ABC-1", "merged", actor="user", reason="my call", clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "merged"


def test_unknown_actor_is_refused(conn):
    sync(conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    with pytest.raises(board.BoardError, match="may not move"):
        board.move_stage(conn, "ABC-1", "pr_open", actor="some-bot", reason="x", clock=clock)


def test_workflow_statuses_survive_a_sync_without_tickets_in_them(conn):
    sync(conn, payload(projects=[{**PROJECT, "statuses": STATUSES + [
        {"name": "Needs refinement", "category": "new", "source": "workflow"}]}], tickets=[]))
    fresh = payload(projects=[{**PROJECT, "statuses": [{"name": "Ready for dev", "category": "new",
                                                       "source": "observed"}]}], tickets=[])
    board.carry_workflow_statuses(conn, fresh)
    sync(conn, fresh)
    names = {r["name"] for r in conn.execute("select name from project_status where project_key='ABC'")}
    assert "Needs refinement" in names


def test_assignee_name_and_links_are_stored(conn):
    t = ticket("ABC-7", status="Ready for dev")
    t["assignee_name"] = "Pat Tester"
    t["links"] = [{"type": "blocks", "direction": "outward", "key": "ABC-8", "summary": "Next", "status": "To Do"}]
    board.apply_payload(conn, payload(tickets=[t]))
    row = conn.execute("select assignee_name from ticket where key = 'ABC-7'").fetchone()
    assert row["assignee_name"] == "Pat Tester"
    link = conn.execute("select * from ticket_link where ticket_key = 'ABC-7'").fetchone()
    assert (link["other_key"], link["link_type"], link["other_summary"]) == ("ABC-8", "blocks", "Next")


def test_a_status_named_open_is_the_to_do_stage(conn):
    from wf import board as _b
    assert _b._stage_for(conn, "ABC", "Open") == "backlog"
    assert _b._stage_for(conn, "ABC", "Backlog") == "backlog"
    assert _b._stage_for(conn, "ABC", "PR open") == "pr_open"
