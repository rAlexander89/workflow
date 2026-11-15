import pytest

from wf.events import StatusChange
from wf.orchestrator import LifecycleStore, Supervisor, route


def change(status, key="ENG-1", frm="x"):
    return StatusChange(issue_key=key, history_id="1", from_status=frm, to_status=status, created="")


@pytest.mark.parametrize(
    "status, action, owner",
    [
        ("Backlog", "none", None),
        ("Needs refinement", "dispatch", "product"),
        ("In refinement", "dispatch", "planning"),
        ("Ready for dev", "dispatch", "dev"),
        ("Dev in progress", "dispatch", "dev"),
        ("PR open", "dispatch", "pr_review"),
        ("PR in review", "dispatch", "pr_review"),
        ("Ready for QA", "dispatch", "qa"),
        ("QA approved", "merge", "harness:merge"),
        ("Merged to main", "none", None),
        ("Regression testing", "dispatch", "qa"),
        ("Deployed to prod", "none", None),
    ],
)
def test_each_status_routes_to_its_owner(status, action, owner):
    d = route(status)
    assert (d.action, d.owner) == (action, owner)


def test_unknown_status_is_reported_not_guessed():
    assert route("Blocked by alien").action == "unknown"


def test_hotfix_label_moves_dev_work_to_on_call():
    d = route("Ready for dev", {"hotfix"})
    assert (d.action, d.owner) == ("dispatch", "oncall")


def test_hotfix_review_uses_planning():
    assert route("Hotfix review", {"hotfix"}).owner == "planning"


def test_non_hotfix_labels_do_not_change_routing():
    assert route("Ready for dev", {"perf", "bug"}).owner == "dev"


def test_supervisor_records_and_dispatches(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    calls = []

    def dispatch(owner, key, status):
        calls.append((owner, key, status))
        return True

    sup = Supervisor(store, dispatch)
    d = sup.handle(change("Ready for dev"))
    assert d.action == "dispatch"
    assert calls == [("dev", "ENG-1", "Ready for dev")]
    row = store.history("ENG-1")[0]
    assert row["action"] == "dispatch" and row["owner"] == "dev" and row["dispatched"] == 1


def test_default_supervisor_records_handoff_but_dispatches_nothing(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    Supervisor(store).handle(change("Ready for dev"))
    assert store.history("ENG-1")[0]["dispatched"] == 0


def test_merge_is_a_harness_action(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    seen = []
    Supervisor(store, lambda o, k, s: seen.append(o) or True).handle(change("QA approved"))
    assert seen == ["harness:merge"]


def test_kill_switch_halts_everything(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    seen = []
    sup = Supervisor(store, lambda o, k, s: seen.append(o) or True)
    store.set_kill(True)
    d = sup.handle(change("Ready for dev"))
    assert d.action == "halt" and seen == []
    store.set_kill(False)
    assert sup.handle(change("Ready for dev")).action == "dispatch"


def test_loop_guard_escalates_a_bouncing_ticket(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    sup = Supervisor(store, lambda o, k, s: True, max_dispatches_per_status=3)
    actions = [sup.handle(change("Dev in progress")).action for _ in range(4)]
    assert actions == ["dispatch", "dispatch", "dispatch", "escalate"]


def test_loop_guard_is_per_ticket(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    sup = Supervisor(store, lambda o, k, s: True, max_dispatches_per_status=1)
    assert sup.handle(change("Dev in progress", key="A-1")).action == "dispatch"
    assert sup.handle(change("Dev in progress", key="B-1")).action == "dispatch"


def test_cli_route_and_kill(tmp_path, capsys):
    from wf import cli

    assert cli.main(["route", "Ready for dev", "--hotfix"]) == 0
    assert "oncall" in capsys.readouterr().out
    db = str(tmp_path / "wf.sqlite")
    assert cli.main(["kill", "on", "--db", db]) == 0
    assert cli.main(["kill", "status", "--db", db]) == 0
    assert "ON" in capsys.readouterr().out
    assert cli.main(["kill", "off", "--db", db]) == 0
