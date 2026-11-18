"""The stage tables and thresholds, pinned, and the order the pipeline reports why a ticket is stuck."""
import pytest

from tests.builders import PROJECT_WITH_MERGED, insert_ticket, payload, set_stage, sync, ticket
from wf.domain.delivery import merge_gate, roles, routing
from wf.domain.execution.run_history import AgentRunRecord


def test_stage_owner_table():
    assert routing.STAGE_OWNERS == {
        "backlog": None,
        "in_refinement": "planning",
        "hotfix_review": "planning",
        "ready_for_dev": "dev",
        "in_dev": "dev",
        "pr_open": "pr_review",
        "pr_in_review": "pr_review",
        "ready_for_perf_review": "perf_review",
        "ready_for_qa": "qa",
        "qa_approved": "harness:merge",
        "merged": None,
        "regression": "qa",
        "performance": "performance",
        "deployed": None,
    }
    assert routing.HOTFIX_LABEL == "hotfix"


def test_stage_role_table():
    assert roles.STAGE_ROLE == {
        "in_refinement": "product",
        "hotfix_review": "staff",
        "ready_for_dev": "dev",
        "in_dev": "dev",
        "pr_open": "pr_review",
        "pr_in_review": "pr_review",
        "ready_for_perf_review": "perf_review",
        "ready_for_qa": "qa",
        "merged": None,
        "regression": "qa",
        "performance": "performance",
    }
    assert roles.REFINEMENT_STAGES == ("in_refinement",)


def test_thresholds_and_stage_groups():
    assert roles.MAX_RUNS_PER_STAGE == 3
    assert roles.MAX_REFINEMENT_ROUNDS == 7
    assert roles.DONE_FOR_BLOCKING == ("merged", "regression", "performance", "deployed")
    assert roles.PAUSABLE == ("in_dev", "pr_open", "pr_in_review")
    assert roles.REQUIRED_APPROVALS == {"pr_review", "perf_review", "qa"} == merge_gate.REQUIRED_APPROVALS


@pytest.fixture
def pipeline(conn, session, settings):
    sync(conn, payload(projects=[PROJECT_WITH_MERGED], tickets=[ticket("ABC-1", status="Ready for dev")]))
    return session.pipeline(settings)


def flag(conn, flags='["needs-human"]'):
    conn.execute("update ticket set flags = ? where key = 'ABC-1'", (flags,))
    conn.commit()


def test_idle_reason_reports_paused_before_flagged_before_an_inactive_sprint(conn, session, pipeline):
    set_stage(conn, "ABC-1", "in_dev")
    flag(conn)
    conn.execute("update sprint set state = 'closed'")
    conn.commit()
    session.pause.pause("test", 0)
    assert "paused" in pipeline.idle_reason("ABC-1")["why"]
    session.pause.resume(1)
    assert "Flagged" in pipeline.idle_reason("ABC-1")["why"]
    flag(conn, "[]")
    assert "active sprint" in pipeline.idle_reason("ABC-1")["why"]


def test_a_flagged_ticket_with_no_owner_stage_is_still_reported_as_flagged(conn, pipeline):
    set_stage(conn, "ABC-1", "deployed")
    flag(conn)
    reason = pipeline.idle_reason("ABC-1")
    assert reason["can_run"] is False and "Flagged" in reason["why"]


def test_idle_reason_after_the_refinement_round_limit_lets_a_person_run_it(conn, session, pipeline):
    set_stage(conn, "ABC-1", "in_refinement")
    for i in range(roles.MAX_REFINEMENT_ROUNDS):
        session.agent_runs.record(AgentRunRecord("ABC-1", "in_refinement", "product", 1000 + i, 0, False, "x"))
    reason = pipeline.idle_reason("ABC-1")
    assert reason["can_run"] is True and "refinement rounds" in reason["why"]


def test_idle_reason_after_repeated_failures_lets_a_person_run_it(conn, session, pipeline):
    set_stage(conn, "ABC-1", "in_dev")
    for i in range(roles.MAX_RUNS_PER_STAGE):
        session.agent_runs.record(AgentRunRecord("ABC-1", "in_dev", "dev", 1000 + i, 1, False, "x"))
    reason = pipeline.idle_reason("ABC-1")
    assert reason["can_run"] is True and "failed here" in reason["why"]


def test_a_ready_ticket_with_an_unmerged_prerequisite_is_waiting_and_cannot_be_run(conn, tickets, pipeline):
    insert_ticket(conn, "ABC-0", "pr_open")
    tickets.add_block("ABC-0", "ABC-1", actor="staff", reason="needs the first")
    set_stage(conn, "ABC-1", "ready_for_dev")
    reason = pipeline.idle_reason("ABC-1")
    assert reason["can_run"] is False and "ABC-0" in reason["why"]
