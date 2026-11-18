"""Domain events: the aggregate records them, the history stores them, and the bus delivers them."""
import pytest

from tests.fakes import MemoryBoardState, MemoryBoardUnitOfWork
from wf.application.board.tickets import TicketCommands
from wf.application.events import EventBus
from wf.bootstrap import build_event_bus
from wf.domain.board.errors import BoardError
from wf.domain.board.events import (
    ApprovalRecorded, FlagCleared, SentToJira, TicketBlocked, TicketCommented, TicketFlagged, TicketMoved,
    TicketReordered,
)
from wf.domain.board.ticket import Ticket
from wf.domain.shared.commit import CommitSha, InvalidCommit
from wf.domain.shared.roles import Actor, Role
from wf.domain.shared.stage import Stage, WORKFLOW_ORDER
from wf.domain.shared.ticket_key import InvalidTicketKey, TicketKey


def ticket(stage=Stage.READY_FOR_DEV):
    return Ticket("K-1", "P", stage)


def test_each_change_records_one_typed_event_that_reads_as_a_history_line():
    t = ticket()
    t.move_to(Stage.IN_DEV, Role.DEV, "pickup", 5)
    t.flag("blocked", Role.DEV, "why", 6)
    t.clear_flag("blocked", Actor.USER, 7)
    t.comment(Role.QA, "x" * 300, 8)
    t.approve(Role.QA, "abc1234", 9)
    t.set_order(2, Role.STAFF, 10)
    t.blocked_by(Ticket("K-0", "P", Stage.IN_DEV), Role.STAFF, "needs it", 11)
    t.sent_to_jira("In Dev", 12)
    events = t.pull_events()
    assert [type(e) for e in events] == [TicketMoved, TicketFlagged, FlagCleared, TicketCommented, ApprovalRecorded,
                                         TicketReordered, TicketBlocked, SentToJira]
    assert t.pull_events() == [] and t.events == []  # handing them over forgets them
    assert events[0].project_key == "P" and events[0].from_stage == Stage.READY_FOR_DEV
    records = [(e.kind, e.actor, e.from_value, e.to_value) for e in (ev.record() for ev in events)]
    assert records == [("stage", "dev", "ready_for_dev", "in_dev"), ("flag", "dev", None, "blocked"),
                       ("unflag", "user", "blocked", None), ("comment", "qa", None, None),
                       ("approve", "qa", None, "abc1234"), ("order", "staff", None, "2"),
                       ("link", "staff", None, "K-0"), ("outbox", "jira-agent", None, "In Dev")]
    assert len(events[3].preview) == 200


def test_a_refused_change_records_nothing():
    t = ticket()
    with pytest.raises(BoardError):
        t.move_to(Stage.DEPLOYED, Role.DEV, "skip", 1)
    assert t.flag("needs-human", Role.QA, "r", 2) is True and t.flag("needs-human", Role.QA, "r", 3) is False
    assert len(t.pull_events()) == 1


def test_the_bus_delivers_each_event_to_its_subscribers_inside_the_unit_of_work():
    seen = []
    bus = EventBus()
    bus.subscribe(TicketMoved, lambda e, uow: seen.append(("moved", e.to_stage, uow)))
    bus.subscribe(TicketFlagged, lambda e, uow: seen.append(("flagged", e.flag, uow)))
    state = MemoryBoardState()
    state.add_ticket("K-1", Stage.READY_FOR_DEV)
    commands = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 1.0, bus)
    commands.move_stage("K-1", Stage.IN_DEV, Role.DEV, "pickup")
    commands.set_flag("K-1", "blocked", Role.DEV, "why")
    assert [(a, b) for a, b, _ in seen] == [("moved", "in_dev"), ("flagged", "blocked")]
    assert all(isinstance(uow, MemoryBoardUnitOfWork) for _, _, uow in seen)


def test_a_handler_that_fails_rolls_the_change_back():
    bus = EventBus()

    def boom(event, uow):
        raise RuntimeError("handler failed")
    bus.subscribe(TicketMoved, boom)
    state = MemoryBoardState()
    state.add_ticket("K-1", Stage.READY_FOR_DEV)
    commands = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 1.0, bus)
    with pytest.raises(RuntimeError):
        commands.move_stage("K-1", Stage.IN_DEV, Role.DEV, "pickup")
    assert state.tickets["K-1"].stage == Stage.READY_FOR_DEV and state.history["K-1"] == []


def test_a_move_queues_the_jira_transition_through_the_standard_subscription():
    state = MemoryBoardState()
    state.add_ticket("K-1", Stage.READY_FOR_DEV)
    state.mappings[("P", "in_dev")] = "In Dev"
    commands = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 1.0, build_event_bus())
    commands.move_stage("K-1", Stage.IN_DEV, Role.DEV, "pickup")
    assert [o["target_stage"] for o in state.outbox] == ["in_dev"]
    bare = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 1.0)  # no subscribers, no Jira move
    bare.move_stage("K-1", Stage.PR_OPEN, Role.DEV, "opened")
    assert len(state.outbox) == 1


def test_value_objects_validate_and_stay_strings():
    assert TicketKey("ABC-12") == "ABC-12" and TicketKey("ABC-12").project == "ABC"
    for bad in ("abc-1", "ABC", "ABC-", "A-1 ", ""):
        with pytest.raises(InvalidTicketKey):
            TicketKey(bad)
    assert CommitSha("ABCDEF1234567").short == "ABCDEF12"
    with pytest.raises(InvalidCommit):
        CommitSha("not a hash")
    assert Stage.IN_DEV == "in_dev" and WORKFLOW_ORDER[0] is Stage.BACKLOG and WORKFLOW_ORDER[-1] is Stage.DEPLOYED
    assert Role.QA == "qa" and Actor.USER == "user"
