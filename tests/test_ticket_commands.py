"""Ticket commands and the Ticket aggregate, tested with in-memory ports: no database."""
import pytest

from tests.fakes import MemoryBoardState, MemoryBoardUnitOfWork
from wf.application.board.tickets import TicketCommands
from wf.domain.board.errors import BoardError
from wf.domain.board.events import TicketEvent, approvals_at, approvals_since_stage, changed_since, perf_requested


@pytest.fixture
def state():
    s = MemoryBoardState()
    s.add_ticket("K-1", "ready_for_dev")
    s.add_ticket("K-2", "in_dev")
    s.mappings[("P", "in_dev")] = "In Dev"
    return s


@pytest.fixture
def commands(state):
    return TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 100.0)


def kinds(state, key):
    return [(e.kind, e.actor, e.from_value, e.to_value) for e in state.history[key]]


def test_a_move_records_the_event_and_queues_jira_when_the_stage_is_mapped(commands, state):
    commands.move_stage("K-1", "in_dev", "dev", "dev pickup")
    assert state.tickets["K-1"].stage == "in_dev"
    assert kinds(state, "K-1") == [("stage", "dev", "ready_for_dev", "in_dev")]
    assert [o["target_stage"] for o in state.outbox] == ["in_dev"]


def test_a_move_to_an_unmapped_stage_queues_nothing(commands, state):
    commands.move_stage("K-2", "pr_open", "dev", "opened")
    assert state.tickets["K-2"].stage == "pr_open" and state.outbox == []


def test_a_newer_move_supersedes_the_older_pending_one(commands, state):
    state.mappings[("P", "pr_open")] = "PR open"
    commands.move_stage("K-2", "pr_open", "dev", "opened")
    commands.move_stage("K-2", "in_dev", "pr_review", "kicked back")
    assert [(o["target_stage"], o["status"]) for o in state.outbox] == [("pr_open", "superseded"),
                                                                        ("in_dev", "pending")]


def test_a_move_to_the_same_stage_does_nothing(commands, state):
    commands.move_stage("K-2", "in_dev", "dev", "again")
    assert state.history["K-2"] == [] and state.outbox == []


def test_moves_are_refused_for_the_wrong_actor_unknown_stage_unknown_or_removed_tickets(commands, state):
    with pytest.raises(BoardError, match="may not move"):
        commands.move_stage("K-1", "deployed", "dev", "skip")
    with pytest.raises(BoardError, match="unknown stage"):
        commands.move_stage("K-9", "nowhere", "user", "x")  # the stage is checked before the ticket
    with pytest.raises(BoardError, match="no ticket K-9"):
        commands.move_stage("K-9", "in_dev", "user", "x")
    state.tickets["K-1"].removed = True
    with pytest.raises(BoardError, match="no longer in scope"):
        commands.move_stage("K-1", "in_dev", "user", "x")
    assert state.history["K-1"] == []


def test_flags_are_set_once_and_only_the_user_clears_them(commands, state):
    commands.set_flag("K-1", "needs-human", "qa", "unclear")
    commands.set_flag("K-1", "needs-human", "qa", "again")
    assert state.tickets["K-1"].flags == ["needs-human"] and len(state.history["K-1"]) == 1
    with pytest.raises(BoardError, match="only the user"):
        commands.clear_flag("K-1", "needs-human", "qa")
    commands.clear_flag("K-1", "needs-human", "user")
    commands.clear_flag("K-1", "needs-human", "user")
    assert state.tickets["K-1"].flags == [] and [k[0] for k in kinds(state, "K-1")] == ["flag", "unflag"]
    with pytest.raises(BoardError, match="flag must be one of"):
        commands.set_flag("K-1", "weird", "qa", "x")


def test_comments_need_a_body_and_are_stored_with_an_event(commands, state):
    with pytest.raises(BoardError, match="needs a body"):
        commands.add_comment("K-1", "qa", "  ")
    cid = commands.add_comment("K-1", "qa", "looks fine")
    assert cid == 1 and state.comments[0]["origin"] == "agent"
    assert state.history["K-1"][0].detail == "looks fine"


def test_approvals_are_per_commit_and_per_role(commands, state):
    with pytest.raises(BoardError, match="cannot approve"):
        commands.record_approval("K-1", "dev", "abc")
    with pytest.raises(BoardError, match="needs the commit"):
        commands.record_approval("K-1", "qa", "")
    commands.record_approval("K-1", "qa", "abc")
    commands.record_approval("K-1", "pr_review", "abc")
    commands.record_approval("K-1", "qa", "def")
    assert commands.approvals_at("K-1", "abc") == {"qa", "pr_review"}
    assert commands.approvals_at("K-1", "def") == {"qa"}


def test_blocks_link_both_tickets_and_cannot_be_circular_or_dangling(commands, state):
    with pytest.raises(BoardError, match="block itself"):
        commands.add_block("K-1", "K-1", "staff", "x")
    with pytest.raises(BoardError, match="no ticket K-9"):
        commands.add_block("K-9", "K-1", "staff", "x")
    commands.add_block("K-2", "K-1", "staff", "needs the first")
    assert commands.blockers("K-1") == ["K-2"] and commands.pending_links() == [("K-2", "K-1")]
    commands.mark_linked([("K-2", "K-1")])
    assert commands.pending_links() == []
    assert kinds(state, "K-1") == [("link", "staff", None, "K-2")]


def test_only_the_dev_can_ask_for_a_perf_check_and_order_is_remembered(commands, state):
    with pytest.raises(BoardError, match="only the dev"):
        commands.request_perf_check("K-1", "qa", "x")
    commands.request_perf_check("K-1", "dev", "hot path")
    assert commands.perf_requested("K-1") and not commands.perf_requested("K-2")
    commands.set_order("K-1", 3, "staff")
    commands.set_order("K-1", 1, "staff")
    assert state.tickets["K-1"].sort_order == 1
    assert [(e.from_value, e.to_value) for e in state.history["K-1"] if e.kind == "order"] == [(None, "3"), ("3", "1")]


def test_a_failed_command_leaves_nothing_behind(commands, state):
    state.add_ticket("K-3", "in_dev")
    with pytest.raises(BoardError):
        commands.add_block("K-3", "K-9", "staff", "x")
    assert state.history["K-3"] == [] and commands.pending_links() == []


def test_history_questions_are_pure():
    h = [TicketEvent("K", 1, "harness", "stage", "a", "regression", ""),
         TicketEvent("K", 2, "qa", "approve", None, "abc", "abc"),
         TicketEvent("K", 0, "pr_review", "approve", None, "abc", "abc"),
         TicketEvent("K", 3, "dev", "perf_request", None, "requested", "")]
    assert approvals_at(h, "abc") == {"qa", "pr_review"}
    assert approvals_since_stage(h, "abc", "regression") == {"qa"}
    assert approvals_since_stage(h, "abc", "never") == {"qa", "pr_review"}
    assert perf_requested(h) and not perf_requested(h[:2])
    assert changed_since(h, 2, "qa") is True and changed_since(h[1:2], 2, "qa") is False


# --- the outbox -------------------------------------------------------------------------------

from tests.fakes import MemoryBoardState as _State  # noqa: E402
from wf.application.tracker.outbox import OutboxService  # noqa: E402


@pytest.fixture
def outbox(state):
    state.tickets["K-1"].jira_status = "Ready for dev"
    state.mappings[("P", "in_dev")] = "In Dev"
    state.mappings[("P", "pr_open")] = "PR open"
    return OutboxService(lambda: MemoryBoardUnitOfWork(state), lambda: 200.0)


def seen(**status):
    return {"K-1": {"status": status.get("status", "Ready for dev"),
                    "transitions": status.get("transitions", [{"id": "11", "to": "In Dev"}])}}


def test_the_plan_asks_for_what_it_has_not_seen_then_sends_an_allowed_move(commands, outbox, state):
    commands.move_stage("K-1", "in_dev", "dev", "pickup")
    assert outbox.plan({})["need"] == ["K-1"]
    plan = outbox.plan(seen())
    assert plan["send"] == [{"id": 1, "key": "K-1", "transition_id": "11", "to_status": "In Dev"}]
    assert plan["decided"] == {"sending": 1}
    assert outbox.plan(seen())["send"] == []  # it is never sent twice


def test_the_plan_skips_blocks_or_flags_a_conflict(commands, outbox, state):
    commands.move_stage("K-1", "in_dev", "dev", "pickup")
    assert outbox.plan(seen(status="In Dev"))["decided"] == {"skipped": 1}
    assert state.tickets["K-1"].jira_status == "In Dev"

    commands.move_stage("K-1", "pr_open", "dev", "opened")
    state.tickets["K-1"].jira_status = "In Dev"
    assert outbox.plan(seen(status="In Dev", transitions=[]))["decided"] == {"blocked": 1}

    outbox.retry(2)
    plan = outbox.plan(seen(status="Someone moved it"))
    assert plan["decided"] == {"conflict": 1} and state.tickets["K-1"].flags == ["needs-human"]


def test_an_ack_makes_a_sent_item_final_and_a_stuck_one_can_be_retried(commands, outbox, state):
    commands.move_stage("K-1", "in_dev", "dev", "pickup")
    outbox.plan(seen())
    assert outbox.ack([{"id": 1, "ok": True, "jira_status": "In Dev", "response": {"x": 1}}, {"id": 99}]) == {
        "sent": 1, "failed": 0, "ignored": 1}
    assert state.tickets["K-1"].jira_status == "In Dev"
    assert ("outbox", "jira-agent", None, "In Dev") in kinds(state, "K-1")
    with pytest.raises(BoardError, match="only blocked, conflict, failed, sending"):
        outbox.retry(1)
    with pytest.raises(BoardError, match="no outbox item 7"):
        outbox.retry(7)


# --- the pipeline service, with every port in memory ------------------------------------------------

from tests.fakes import MemoryAgentRuns, MemoryBoardQueries, MemoryPauseState  # noqa: E402
from wf.application.delivery.pipeline import PipelineService  # noqa: E402


class FakeWorkspace:
    def __init__(self, heads):
        self.heads = heads

    def branch_head(self, key):
        return self.heads.get(key)


class FakeDeploys:
    def __init__(self, deployed=()):
        self.deployed = set(deployed)

    def dev_deployed(self, key):
        return key in self.deployed


@pytest.fixture
def pipeline(state, commands):
    state.sprint_state.update({"K-1": "active", "K-2": "active"})
    svc = PipelineService(commands=commands, queries=MemoryBoardQueries(state), pause=MemoryPauseState(),
                          runs=MemoryAgentRuns(), workspace=FakeWorkspace({"K-1": "a" * 40, "K-2": "b" * 40}),
                          deploys=FakeDeploys())
    return svc


def test_advance_moves_a_reviewed_ticket_on_and_a_sprint_backlog_ticket_into_refinement(pipeline, commands, state):
    state.add_ticket("K-3", "backlog")
    state.sprint_state["K-3"] = "active"
    state.tickets["K-2"].stage = "pr_open"
    commands.record_approval("K-2", "pr_review", "b" * 40)
    assert pipeline.advance() == ["K-3", "K-2"]
    assert state.tickets["K-3"].stage == "in_refinement" and state.tickets["K-2"].stage == "ready_for_perf_review"
    assert ("stage", "harness", "pr_open", "ready_for_perf_review") in kinds(state, "K-2")


def test_advance_does_nothing_while_paused_and_waits_for_the_deploy(pipeline, state):
    state.tickets["K-1"].stage = "merged"
    assert pipeline.advance() == []
    pipeline._deploys.deployed.add("K-1")
    pipeline._pause.pause("by a person", 5)
    assert pipeline.advance() == [] and state.tickets["K-1"].stage == "merged"
    pipeline._pause.resume(6)
    assert pipeline.advance() == ["K-1"] and state.tickets["K-1"].stage == "regression"


def test_an_approval_of_an_old_commit_does_not_advance_a_ticket(pipeline, commands, state):
    state.tickets["K-2"].stage = "pr_open"
    commands.record_approval("K-2", "pr_review", "0" * 40)
    assert pipeline.advance() == [] and state.tickets["K-2"].stage == "pr_open"


def test_a_pass_plans_one_dev_and_explains_a_stuck_ticket(pipeline, state):
    state.tickets["K-2"].stage = "ready_for_dev"
    plan = pipeline.plan_pass()
    assert [(a.key, a.role, a.skipped) for a in plan] == [("K-1", "dev", None), ("K-2", "dev", "dev is busy with K-1")]
    assert pipeline.idle_reason("K-1") is None
    state.tickets["K-2"].flags.append("blocked")
    assert "Flagged: blocked" in pipeline.idle_reason("K-2")["why"]
