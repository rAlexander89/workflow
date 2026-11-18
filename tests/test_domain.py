"""The domain rules, tested with no database, git, or network."""
from wf.domain.board import moves, stages
from wf.domain.delivery import checks, roles, routing
from wf.domain.tracker import changelog


def test_the_user_may_move_anything_and_others_only_listed_moves():
    assert moves.may_move("user", "deployed", "backlog")
    assert moves.may_move("dev", "ready_for_dev", "in_dev")
    assert not moves.may_move("dev", "in_dev", "deployed")
    assert not moves.may_move("qa", None, "in_dev")


def test_a_status_maps_to_one_stage_and_ambiguity_is_left_out():
    assert stages.stages_for("Ready for Development") == ["ready_for_dev"]
    rows, problems = stages.map_project(["To Do", "In Refinement", "Done", "Merged"])
    by_stage = {r.stage: r.status_name for r in rows}
    assert by_stage["backlog"] == "To Do" and by_stage["deployed"] == "Done" and by_stage["merged"] == "Merged"
    assert by_stage["regression"] is None and not problems
    _, problems = stages.map_project(["Performance Regression"])
    assert problems


def test_routing_hotfix_goes_to_oncall_and_unmapped_is_unknown():
    mapping = {"Ready for dev": "ready_for_dev", "QA Approved": "qa_approved", "Done": "deployed"}
    assert routing.route("Ready for dev", {"hotfix"}, mapping).owner == "oncall"
    assert routing.route("QA Approved", frozenset(), mapping).action == "merge"
    assert routing.route("Done", frozenset(), mapping).action == "none"
    assert routing.route("Nope", frozenset(), mapping).action == "unknown"


def test_refinement_alternates_and_the_limits_are_inclusive():
    assert roles.next_refinement_role(None) == "product"
    assert roles.next_refinement_role("product") == "staff"
    assert roles.next_refinement_role("staff") == "product"
    assert not roles.refinement_limit_reached(6) and roles.refinement_limit_reached(7)
    assert not roles.run_limit_reached(2) and roles.run_limit_reached(3)


def test_merge_needs_every_required_approval():
    assert roles.missing_approvals({"qa"}) == {"pr_review", "perf_review"}
    assert roles.missing_approvals({"qa", "pr_review", "perf_review", "extra"}) == frozenset()


def test_a_prerequisite_is_met_once_it_is_on_dev():
    stage_of = {"A-1": "in_dev", "A-2": "merged", "A-3": "deployed"}
    assert roles.unmet_blockers(["A-1", "A-2", "A-3", "A-9"], stage_of) == ["A-1", "A-9"]


def test_check_states():
    assert checks.rollup_state([]) == "none"
    assert checks.rollup_state([{"status": "COMPLETED", "conclusion": "FAILURE"}]) == "failed"
    assert checks.rollup_state([{"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert checks.rollup_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) == "passed"
    assert checks.pairs_state([("COMPLETED", "CANCELED")]) == "failed"
    assert checks.pairs_state([("RUNNING", "")]) == "pending"
    assert checks.pairs_state([("SUCCESS", "")]) == "passed"


def test_changelog_keeps_only_status_changes_and_the_poll_waits_two_hours():
    body = {"changelog": {"histories": [
        {"id": 7, "created": "t", "items": [{"field": "status", "fromString": "A", "toString": "B"},
                                             {"field": "labels"}]}]},
            "fields": {"labels": ["hotfix"]}}
    changes = changelog.parse_changelog("K-1", body)
    assert [(c.history_id, c.from_status, c.to_status) for c in changes] == [("7", "A", "B")]
    assert changelog.labels_from_body(body) == {"hotfix"}
    assert changelog.issue_key_from_webhook({"issue": {"key": "K-1"}}) == "K-1"
    assert changelog.issue_key_from_webhook({"issue": {}}) is None
    assert changelog.poll_due(None, 100.0)
    assert not changelog.poll_due(0.0, 7199.0) and changelog.poll_due(0.0, 7200.0)


def test_pickup_skip_reasons_in_priority_order():
    from wf.domain.delivery.pickup import skip_reason
    assert skip_reason("closed", ["blocked"], None) == "not in an active sprint"
    assert skip_reason("active", ["blocked", "needs-human"], None) == "flagged: blocked, needs-human"
    assert skip_reason("active", [], None) == "no Jira status is mapped to in_dev"
    assert skip_reason("active", [], "In Dev") is None


def test_every_stage_has_an_owner_so_routing_never_fails():
    from wf.domain.board.stages import STAGES
    from wf.domain.delivery.routing import STAGE_OWNERS, route
    assert set(STAGES) <= set(STAGE_OWNERS)
    for stage in STAGES:
        assert route("Any", frozenset(), {"Any": stage}).action in ("dispatch", "merge", "none")


def test_a_flagged_ticket_is_never_advanced():
    from wf.domain.delivery.pipeline import AdvanceFacts, advance_move
    ok = AdvanceFacts("regression", head="a" * 40, approved_since_regression=frozenset({"qa"}))
    assert advance_move(ok) is not None
    assert advance_move(AdvanceFacts("regression", flagged=True, head="a" * 40,
                                     approved_since_regression=frozenset({"qa"}))) is None
    assert advance_move(AdvanceFacts("merged", flagged=True, dev_deployed=True)) is None
