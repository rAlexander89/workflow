import json

import pytest

from wf import board, pickup
from tests.test_board import NOW, clock, payload, sprint, sync, ticket, ticket_row, conn  # noqa: F401


def ready(key, **extra):
    return ticket(key, status="Ready for dev", **extra)


def test_dry_run_moves_nothing(conn):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    plan = pickup.plan_pickup(conn, apply=False, clock=clock)
    assert [i.key for i in plan.items] == ["ABC-1"]
    assert plan.items[0].skipped is None
    assert plan.items[0].jira_target == "Dev in progress"
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"
    assert conn.execute("select count(*) from outbox").fetchone()[0] == 0


def test_apply_moves_to_in_dev_and_queues_jira_move(conn):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    pickup.plan_pickup(conn, apply=True, clock=clock)
    assert ticket_row(conn, "ABC-1")["stage"] == "in_dev"
    rows = conn.execute("select target_stage, actor from outbox").fetchall()
    assert [tuple(r) for r in rows] == [("in_dev", "dev")]


def test_skips_tickets_outside_active_sprint(conn):
    sync(conn, payload(sprints=[sprint("101", "active"), sprint("55", "closed")],
                       tickets=[ready("ABC-1", sprint_id="55")]))
    plan = pickup.plan_pickup(conn, apply=True, clock=clock)
    assert plan.items[0].skipped == "not in an active sprint"
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_skips_flagged_tickets(conn):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    board.set_flag(conn, "ABC-1", "needs-human", "user", "look first", clock=clock)
    plan = pickup.plan_pickup(conn, apply=True, clock=clock)
    assert plan.items[0].skipped.startswith("flagged")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_gateway_binds_role(conn):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    gw = pickup.Gateway(conn, "qa", clock=clock)
    with pytest.raises(board.BoardError):
        gw.move("ABC-1", "in_dev", "wrong role for this move")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_gateway_needs_role(conn):
    with pytest.raises(pickup.GatewayError):
        pickup.Gateway(conn, "")
