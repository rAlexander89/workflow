"""Dev pickup over SQLite: the plan, and what applying it does to the board and the outbox."""
import pytest

from tests.builders import payload, sprint, sync, ticket, ticket_row
from wf.application.delivery.pickup import GatewayError, RoleGateway
from wf.domain.board.errors import BoardError


def ready(key, **extra):
    return ticket(key, status="Ready for dev", **extra)


def test_dry_run_moves_nothing(conn, session):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    plan = session.pickup().plan(apply=False)
    assert [i.key for i in plan.items] == ["ABC-1"]
    assert plan.items[0].skipped is None
    assert plan.items[0].jira_target == "Dev in progress"
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"
    assert conn.execute("select count(*) from outbox").fetchone()[0] == 0


def test_apply_moves_to_in_dev_and_queues_jira_move(conn, session):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    session.pickup().plan(apply=True)
    assert ticket_row(conn, "ABC-1")["stage"] == "in_dev"
    rows = conn.execute("select target_stage, actor from outbox").fetchall()
    assert [tuple(r) for r in rows] == [("in_dev", "dev")]


def test_skips_tickets_outside_active_sprint(conn, session):
    sync(conn, payload(sprints=[sprint("101", "active"), sprint("55", "closed")],
                       tickets=[ready("ABC-1", sprint_id="55")]))
    plan = session.pickup().plan(apply=True)
    assert plan.items[0].skipped == "not in an active sprint"
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_skips_flagged_tickets(conn, session, tickets):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    tickets.set_flag("ABC-1", "needs-human", "user", "look first")
    plan = session.pickup().plan(apply=True)
    assert plan.items[0].skipped.startswith("flagged")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_gateway_binds_role(conn, tickets, queries):
    sync(conn, payload(tickets=[ready("ABC-1")]))
    gw = RoleGateway(tickets, queries, "qa")
    with pytest.raises(BoardError):
        gw.move("ABC-1", "in_dev", "wrong role for this move")
    assert ticket_row(conn, "ABC-1")["stage"] == "ready_for_dev"


def test_gateway_needs_role(tickets, queries):
    with pytest.raises(GatewayError):
        RoleGateway(tickets, queries, "")
