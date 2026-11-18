"""Pins today's behavior of the pipeline rules, so they can move into the domain unchanged.

These cover what the other tests leave open: the stage tables, the thresholds, and the order in
which `idle_reason` reports why a ticket is stuck. A change that breaks one of these is a change
in behavior, not a refactor.
"""
from tests.support import agents
from tests.support import board
from tests.support import merge
from tests.support import orchestrator
from tests.support import pause
from tests.test_agents import _add_ticket, _set_stage, conn, repo  # noqa: F401  (fixtures)


def test_stage_owner_table():
    assert orchestrator.STAGE_OWNERS == {
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
    assert orchestrator.HOTFIX_LABEL == "hotfix"


def test_stage_role_table():
    assert agents.STAGE_ROLE == {
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
    assert agents.REFINEMENT_STAGES == ("in_refinement",)


def test_thresholds_and_stage_groups():
    assert agents.MAX_RUNS_PER_STAGE == 3
    assert agents.MAX_REFINEMENT_ROUNDS == 7
    assert agents.DONE_FOR_BLOCKING == ("merged", "regression", "performance", "deployed")
    assert agents.PAUSABLE == ("in_dev", "pr_open", "pr_in_review")
    assert merge.REQUIRED_APPROVALS == {"pr_review", "perf_review", "qa"}


def _flag(conn, flags='["needs-human"]'):
    conn.execute("update ticket set flags = ? where key = 'ABC-1'", (flags,))
    conn.commit()


def test_idle_reason_reports_paused_before_flagged_before_an_inactive_sprint(conn):
    _set_stage(conn, "in_dev")
    _flag(conn)
    conn.execute("update sprint set state = 'closed'")
    conn.commit()
    pause.pause(conn)
    assert "paused" in agents.idle_reason(conn, "ABC-1")["why"]
    pause.resume(conn)
    assert "Flagged" in agents.idle_reason(conn, "ABC-1")["why"]
    _flag(conn, "[]")
    assert "active sprint" in agents.idle_reason(conn, "ABC-1")["why"]


def test_a_flagged_ticket_with_no_owner_stage_is_still_reported_as_flagged(conn):
    _set_stage(conn, "deployed")
    _flag(conn)
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is False and "Flagged" in reason["why"]


def test_idle_reason_after_the_refinement_round_limit_lets_a_person_run_it(conn):
    _set_stage(conn, "in_refinement")
    for i in range(agents.MAX_REFINEMENT_ROUNDS):
        conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                     "values ('ABC-1','in_refinement','product',?,0,0,'x')", (1000 + i,))
    conn.commit()
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is True and "refinement rounds" in reason["why"]


def test_idle_reason_after_repeated_failures_lets_a_person_run_it(conn):
    _set_stage(conn, "in_dev")
    for i in range(agents.MAX_RUNS_PER_STAGE):
        conn.execute("insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
                     "values ('ABC-1','in_dev','dev',?,1,0,'x')", (1000 + i,))
    conn.commit()
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is True and "failed here" in reason["why"]


def test_a_ready_ticket_with_an_unmerged_prerequisite_is_waiting_and_cannot_be_run(conn):
    _add_ticket(conn, "ABC-0", "pr_open")
    board.add_block(conn, "ABC-0", "ABC-1", actor="staff", reason="needs the first")
    _set_stage(conn, "ready_for_dev")
    reason = agents.idle_reason(conn, "ABC-1")
    assert reason["can_run"] is False and "ABC-0" in reason["why"]
