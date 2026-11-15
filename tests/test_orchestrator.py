import pytest

from wf.events import StatusChange
from wf.orchestrator import LifecycleStore, Supervisor, route

# Test data only. In a real run the mapping comes from the board, per team.
MAPPING = {
    "Backlog": "backlog",
    "Needs refinement": "needs_refinement",
    "In refinement": "in_refinement",
    "Hotfix review": "hotfix_review",
    "Ready for dev": "ready_for_dev",
    "Dev in progress": "in_dev",
    "PR open": "pr_open",
    "PR in review": "pr_in_review",
    "Ready for QA": "ready_for_qa",
    "QA approved": "qa_approved",
    "Merged to main": "merged",
    "Regression testing": "regression",
    "Deployed to prod": "deployed",
}


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
def test_each_status_routes_to_its_stage_owner(status, action, owner):
    d = route(status, status_to_stage=MAPPING)
    assert (d.action, d.owner) == (action, owner)


def test_unmapped_status_is_reported_not_guessed():
    d = route("Blocked by alien", status_to_stage=MAPPING)
    assert d.action == "unknown" and "not mapped" in d.reason


def test_no_mapping_means_nothing_routes():
    assert route("Ready for dev").action == "unknown"
    assert route("Ready for dev", status_to_stage={}).action == "unknown"


def test_hotfix_label_moves_dev_work_to_on_call():
    d = route("Ready for dev", {"hotfix"}, MAPPING)
    assert (d.action, d.owner) == ("dispatch", "oncall")


def test_hotfix_review_uses_planning():
    assert route("Hotfix review", {"hotfix"}, MAPPING).owner == "planning"


def test_non_hotfix_labels_do_not_change_routing():
    assert route("Ready for dev", {"perf", "bug"}, MAPPING).owner == "dev"


def test_supervisor_records_and_dispatches(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    calls = []

    def dispatch(owner, key, status):
        calls.append((owner, key, status))
        return True

    sup = Supervisor(store, dispatch, status_to_stage=lambda: MAPPING)
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
    Supervisor(store, lambda o, k, s: seen.append(o) or True, status_to_stage=lambda: MAPPING).handle(
        change("QA approved"))
    assert seen == ["harness:merge"]


def test_kill_switch_halts_everything(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    seen = []
    sup = Supervisor(store, lambda o, k, s: seen.append(o) or True, status_to_stage=lambda: MAPPING)
    store.set_kill(True)
    d = sup.handle(change("Ready for dev"))
    assert d.action == "halt" and seen == []
    store.set_kill(False)
    assert sup.handle(change("Ready for dev")).action == "dispatch"


def test_loop_guard_escalates_a_bouncing_ticket(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    sup = Supervisor(store, lambda o, k, s: True, max_dispatches_per_status=3, status_to_stage=lambda: MAPPING)
    actions = [sup.handle(change("Dev in progress")).action for _ in range(4)]
    assert actions == ["dispatch", "dispatch", "dispatch", "escalate"]


def test_loop_guard_is_per_ticket(tmp_path):
    store = LifecycleStore(tmp_path / "wf.sqlite")
    sup = Supervisor(store, lambda o, k, s: True, max_dispatches_per_status=1, status_to_stage=lambda: MAPPING)
    assert sup.handle(change("Dev in progress", key="A-1")).action == "dispatch"
    assert sup.handle(change("Dev in progress", key="B-1")).action == "dispatch"


def test_cli_route_and_kill(tmp_path, capsys):
    from wf import cli
    from wf.board import connect, sync_file
    import json

    db = tmp_path / "wf.sqlite"
    payload = tmp_path / "sync.json"
    payload.write_text(json.dumps({
        "kind": "active",
        "projects": [{"key": "ABC", "name": "Example", "statuses": [
            {"name": "Ready for dev", "category": "new"},
            {"name": "Dev in progress", "category": "indeterminate"},
        ]}],
    }))
    conn = connect(db)
    sync_file(conn, payload)
    conn.close()

    assert cli.main(["route", "Ready for dev", "--hotfix", "--db", str(db)]) == 0
    assert "oncall" in capsys.readouterr().out
    assert cli.main(["kill", "on", "--db", str(db)]) == 0
    assert cli.main(["kill", "status", "--db", str(db)]) == 0
    assert "ON" in capsys.readouterr().out
    assert cli.main(["kill", "off", "--db", str(db)]) == 0
