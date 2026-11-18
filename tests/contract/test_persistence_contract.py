"""Each persistence port is tested once and run against the SQLite adapter and the in-memory fake."""
import pytest

from tests import fakes
from wf.adapters.persistence.sqlite.schema import connect
from wf.adapters.persistence.sqlite.agent_runs import SqliteAgentRuns
from wf.adapters.persistence.sqlite.changes import SqliteChangeLog
from wf.adapters.persistence.sqlite.lifecycle import SqliteLifecycleLog
from wf.adapters.persistence.sqlite.pause import SqlitePauseState
from wf.adapters.persistence.sqlite.runs import SqliteRunRegistry
from wf.adapters.persistence.sqlite.usage import SqliteUsageLog
from wf.domain.delivery.routing import Decision
from wf.domain.execution.run_history import AgentRunRecord
from wf.domain.execution.usage import UsageRecord
from wf.domain.tracker.changelog import StatusChange


@pytest.fixture(params=["sqlite", "memory"])
def runs(request, tmp_path):
    return SqliteRunRegistry(tmp_path / "wf.sqlite") if request.param == "sqlite" else fakes.MemoryRunRegistry()


@pytest.fixture(params=["sqlite", "memory"])
def usage(request, tmp_path):
    return SqliteUsageLog(tmp_path / "wf.sqlite") if request.param == "sqlite" else fakes.MemoryUsageLog()


@pytest.fixture(params=["sqlite", "memory"])
def changes(request, tmp_path):
    return SqliteChangeLog(tmp_path / "wf.sqlite") if request.param == "sqlite" else fakes.MemoryChangeLog()


@pytest.fixture(params=["sqlite", "memory"])
def lifecycle(request, tmp_path):
    return SqliteLifecycleLog(tmp_path / "wf.sqlite") if request.param == "sqlite" else fakes.MemoryLifecycleLog()


def change(key="K-1", hid="1", a="A", b="B"):
    return StatusChange(key, hid, a, b, "t")


def test_runs_are_created_updated_and_listed(runs):
    assert runs.get("r1") is None
    runs.create("r1", "add search")
    assert runs.get("r1").status == "running" and runs.get("r1").request == "add search"
    runs.set_status("r1", "done")
    assert runs.get("r1").status == "done"
    assert [r.id for r in runs.list()] == ["r1"]


def test_usage_is_recorded_audited_and_ranked(usage):
    usage.record(UsageRecord("product", "m", input_tokens=100, output_tokens=20, cache_read_tokens=50, ticket="K-1"))
    usage.record(UsageRecord("product", "m", input_tokens=300, output_tokens=40))
    usage.record(UsageRecord("dev", "m", input_tokens=10, output_tokens=5))
    audit = {a.agent: a for a in usage.audit()}
    assert audit["product"].calls == 2 and audit["product"].avg_input == 200
    assert audit["product"].total_output == 60 and 0 < audit["product"].cache_hit_ratio < 1
    assert audit["dev"].cache_hit_ratio == 0.0
    top = usage.largest_calls(1)
    assert len(top) == 1 and top[0]["agent"] == "product" and top[0]["input_tokens"] == 300


def test_negative_token_counts_are_refused(usage):
    with pytest.raises(ValueError):
        usage.record(UsageRecord("product", "m", input_tokens=-1, output_tokens=0))


def test_a_status_change_is_new_only_once(changes):
    first = changes.record_new([change(hid="1"), change(hid="2")])
    assert [c.history_id for c in first] == ["1", "2"]
    assert changes.record_new([change(hid="2"), change(hid="3")]) == [change(hid="3")]


def test_the_last_webhook_time_is_remembered(changes):
    assert changes.last_webhook_at() is None
    changes.mark_webhook(1234.0)
    assert changes.last_webhook_at() == 1234.0


def test_lifecycle_counts_only_dispatched_decisions_and_keeps_history(lifecycle):
    d = Decision("dispatch", "dev", "r")
    lifecycle.record(change(), d, dispatched=True)
    lifecycle.record(change(hid="2"), d, dispatched=False)
    assert lifecycle.dispatch_count("K-1", "B") == 1
    assert lifecycle.dispatch_count("K-1", "Z") == 0
    rows = lifecycle.history("K-1")
    assert [r["dispatched"] for r in rows] == [1, 0] and rows[0]["owner"] == "dev"


def test_the_kill_switch_defaults_off(lifecycle):
    assert lifecycle.killed() is False
    lifecycle.set_kill(True)
    assert lifecycle.killed() is True
    lifecycle.set_kill(False)
    assert lifecycle.killed() is False


@pytest.fixture(params=["sqlite", "memory"])
def pause_state(request, tmp_path):
    if request.param == "sqlite":
        return SqlitePauseState(connect(tmp_path / "wf.sqlite"))
    return fakes.MemoryPauseState()


@pytest.fixture(params=["sqlite", "memory"])
def agent_runs(request, tmp_path):
    if request.param == "sqlite":
        return SqliteAgentRuns(connect(tmp_path / "wf.sqlite"))
    return fakes.MemoryAgentRuns()


def test_pause_is_idempotent_and_remembers_when(pause_state):
    assert pause_state.is_paused() is False and pause_state.paused_since() is None
    assert pause_state.resume(5) is False
    assert pause_state.pause("by a person", 100) is True
    assert pause_state.pause("again", 200) is False
    assert pause_state.is_paused() is True and pause_state.paused_since() == 100
    assert pause_state.resume(300) is True
    assert pause_state.is_paused() is False and pause_state.paused_since() is None


def _run(runs, key="K-1", stage="in_dev", role="dev", at=1, code=0, timed_out=False):
    runs.record(AgentRunRecord(key, stage, role, at, code, timed_out, "out"))


def test_agent_runs_come_back_newest_first_and_per_ticket(agent_runs):
    assert agent_runs.runs_for("K-1") == []
    _run(agent_runs, role="dev", at=1, code=1)
    _run(agent_runs, role="pr_review", at=2, code=0)
    _run(agent_runs, key="K-2", stage="qa", role="qa", at=3)
    runs = agent_runs.runs_for("K-1")
    assert [(r.role, r.exit_code, r.started_at) for r in runs] == [("pr_review", 0, 2), ("dev", 1, 1)]
    assert runs[0] == AgentRunRecord("K-1", "in_dev", "pr_review", 2, 0, False, "out")
    assert [r.key for r in agent_runs.runs_for("K-2")] == ["K-2"]


def test_a_timed_out_run_has_no_exit_code_and_is_kept(agent_runs):
    _run(agent_runs, at=4, code=None, timed_out=True)
    run = agent_runs.runs_for("K-1")[0]
    assert run.exit_code is None and run.timed_out is True and run.failed


# --- the ticket repository -----------------------------------------------------------------

from tests.builders import PROJECT, payload, sync, ticket  # noqa: E402
from wf.adapters.persistence.sqlite.board import SqliteTicketRepository  # noqa: E402


@pytest.fixture(params=["sqlite", "memory"])
def tickets(request, tmp_path):
    if request.param == "sqlite":
        conn = connect(tmp_path / "wf.sqlite")
        sync(conn, payload(projects=[PROJECT], tickets=[ticket("ABC-1", status="Ready for dev"),
                                                        ticket("ABC-2", status="Ready for dev")]))
        return SqliteTicketRepository(conn)
    state = fakes.MemoryBoardState()
    state.add_ticket("ABC-1", "ready_for_dev")
    state.add_ticket("ABC-2", "ready_for_dev")
    return fakes.MemoryTicketRepository(state)


def test_a_ticket_is_loaded_changed_and_saved_with_its_events(tickets):
    assert tickets.get("NOPE-1") is None
    t = tickets.get("ABC-1")
    assert t.stage == "ready_for_dev" and t.flags == [] and t.removed is False
    t.move_to("in_dev", "dev", "pickup", 5000)
    t.flag("blocked", "dev", "why", 5001)
    tickets.save(t)
    assert t.events == []
    again = tickets.get("ABC-1")
    assert again.stage == "in_dev" and again.flags == ["blocked"]
    assert [(e.kind, e.at, e.to_value) for e in tickets.history("ABC-1")][-2:] == [("stage", 5000, "in_dev"),
                                                                                  ("flag", 5001, "blocked")]


def test_comments_and_blocking_links(tickets):
    assert tickets.add_comment("ABC-1", "agent", "qa", "hello", 5) >= 1
    tickets.link_blocks("ABC-2", "ABC-1")
    tickets.link_blocks("ABC-2", "ABC-1")
    assert tickets.blockers("ABC-1") == ["ABC-2"] and tickets.blockers("ABC-2") == []
    assert tickets.pending_links() == [("ABC-2", "ABC-1")]
    tickets.mark_linked([("ABC-2", "ABC-1")])
    assert tickets.pending_links() == []
