"""The pipeline's decisions as pure functions: no database, git, or clock."""
from wf.domain.delivery.pipeline import (
    AdvanceFacts, IdleFacts, Move, PassTicket, TicketFacts, advance_move, idle_reason, paused_move, plan_pass,
    work_order,
)

HEAD = "abcdef1234567890"


def test_a_merged_ticket_goes_to_regression_only_once_deployed_to_dev():
    assert advance_move(AdvanceFacts("merged")) is None
    assert advance_move(AdvanceFacts("merged", dev_deployed=True)) == Move("regression", "deployed to dev; regression on dev")


def test_regression_passes_on_a_qa_approval_and_goes_to_performance_if_asked():
    assert advance_move(AdvanceFacts("regression", head=HEAD)) is None
    assert advance_move(AdvanceFacts("regression", head=None, approved_since_regression=frozenset({"qa"}))) is None
    passed = AdvanceFacts("regression", head=HEAD, approved_since_regression=frozenset({"qa"}))
    assert advance_move(passed) == Move("deployed", "regression passed at abcdef12; deployed to dev")
    asked = AdvanceFacts("regression", head=HEAD, approved_since_regression=frozenset({"qa"}), perf_requested=True)
    assert advance_move(asked) == Move("performance", "regression passed; performance requested")


def test_performance_and_pr_review_decisions():
    assert advance_move(AdvanceFacts("performance", head=HEAD)) is None
    done = AdvanceFacts("performance", head=HEAD, approved_since_performance=frozenset({"performance"}))
    assert advance_move(done) == Move("deployed", "performance passed at abcdef12")
    for stage in ("pr_open", "pr_in_review"):
        assert advance_move(AdvanceFacts(stage, head=HEAD)) is None
        ok = AdvanceFacts(stage, head=HEAD, approved_at_head=frozenset({"pr_review"}))
        assert advance_move(ok) == Move("ready_for_perf_review", "PR review approved at abcdef12")
    assert advance_move(AdvanceFacts("pr_open", head=None, approved_at_head=frozenset({"pr_review"}))) is None


def test_a_kicked_back_prerequisite_pauses_the_dependent():
    assert paused_move("K-1") == Move("ready_for_dev", "paused: prerequisite K-1 was sent back to dev")


def t(key, stage, order=None, flags=(), sprint="active"):
    return PassTicket(key, stage, tuple(flags), order, sprint)


def test_work_order_is_in_development_first_then_sprint_order_then_key():
    ordered = work_order([t("K-3", "ready_for_dev"), t("K-2", "ready_for_dev", 2), t("K-1", "ready_for_dev", 1),
                          t("K-9", "in_dev")])
    assert [x.key for x in ordered] == ["K-9", "K-1", "K-2", "K-3"]


def facts(**kw):
    return TicketFacts(**kw)


def test_one_dev_at_a_time_and_a_ready_ticket_waits_for_its_prerequisites():
    tickets = [t("K-1", "in_dev"), t("K-2", "ready_for_dev", 1), t("K-3", "pr_open")]
    plan = plan_pass(tickets, {"K-1": facts(), "K-2": facts(), "K-3": facts()})
    assert [(a.key, a.role, a.skipped) for a in plan] == [
        ("K-1", "dev", None), ("K-2", "dev", "dev is busy with K-1"), ("K-3", "pr_review", None)]

    tickets = [t("K-2", "ready_for_dev", 1), t("K-4", "ready_for_dev", 2)]
    plan = plan_pass(tickets, {"K-2": facts(unmet_blockers=("K-7",)), "K-4": facts()})
    assert [(a.key, a.skipped) for a in plan] == [("K-2", "waiting for K-7 to merge to dev"), ("K-4", None)]

    plan = plan_pass([t("K-2", "ready_for_dev", 1), t("K-4", "ready_for_dev", 2)], {"K-2": facts(), "K-4": facts()})
    assert [(a.key, a.skipped) for a in plan] == [("K-2", None), ("K-4", "dev is busy with K-2")]


def test_flagged_inactive_sprint_and_finished_tickets_are_left_out():
    tickets = [t("K-1", "pr_open", flags=("needs-human",)), t("K-2", "pr_open", sprint="closed"),
               t("K-3", "pr_open"), t("K-4", "deployed"), t("K-5", "in_dev")]
    plan = plan_pass(tickets, {"K-3": facts(finished_and_unchanged=True), "K-5": facts()})
    assert [a.key for a in plan] == ["K-5"]


def test_refinement_alternates_and_stops_for_a_person_after_seven_rounds():
    tickets = [t("K-1", "in_refinement"), t("K-2", "in_refinement")]
    plan = plan_pass(tickets, {"K-1": facts(refinement_role="staff", product_rounds=3),
                               "K-2": facts(refinement_role="product", product_rounds=7)})
    assert (plan[0].role, plan[0].skipped, plan[0].flag_needs_human) == ("staff", None, False)
    assert plan[1].skipped == "7 refinement rounds without agreement; needs a human" and plan[1].flag_needs_human


def test_a_stage_that_failed_three_times_in_a_row_needs_a_human():
    plan = plan_pass([t("K-1", "pr_open")], {"K-1": facts(failed_in_a_row=3)})
    assert plan[0].skipped == "run limit reached for this stage; needs a human" and not plan[0].flag_needs_human
    assert plan[0].to_dict() == {"key": "K-1", "stage": "pr_open", "role": "pr_review",
                                 "skipped": "run limit reached for this stage; needs a human"}


def idle(**kw):
    base = dict(stage="in_dev", flags=(), sprint_state="active", paused=False, role="dev")
    return idle_reason(IdleFacts(**{**base, **kw}))


def test_idle_reasons_are_reported_in_priority_order():
    assert idle(stage=None) is None
    assert "paused" in idle(paused=True, flags=("blocked",), sprint_state="closed")["why"]
    assert "Flagged: blocked" in idle(flags=("blocked",), sprint_state="closed")["why"]
    assert idle(stage="deployed", role=None) is None
    assert "active sprint" in idle(sprint_state="closed")["why"]
    assert idle() is None


def test_idle_reasons_say_who_can_run_it():
    assert idle(stage="in_refinement", role="product", product_rounds=7) == {
        "why": "7 refinement rounds without agreement. A person has to decide.", "can_run": True}
    assert idle(failed_in_a_row=3)["can_run"] is True and "3 runs in a row failed" in idle(failed_in_a_row=3)["why"]
    waiting = idle(stage="ready_for_dev", unmet_blockers=("K-7", "K-8"))
    assert waiting == {"why": "Waiting for K-7, K-8 to reach dev first.", "can_run": False}
    finished = idle(finished_and_unchanged=True)
    assert finished["can_run"] is True and "The dev agent finished its last run here" in finished["why"]
    assert idle(stage="ready_for_dev", finished_and_unchanged=True) is None
