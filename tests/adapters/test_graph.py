from wf.adapters.workflow import langgraph as graph


def test_happy_path_runs_all_stages(tmp_path):
    db = tmp_path / "runs.sqlite"
    first = graph.start(db, "t1", "add search")
    assert first["status"] == "waiting_for_approval"
    assert first["steps"] == ["product", "staff"]
    done = graph.resume(db, "t1", approved=True)
    assert done["status"] == "done"
    assert done["steps"] == ["product", "staff", "gate", "dev", "qa"]


def test_rejection_stops_before_dev(tmp_path):
    db = tmp_path / "runs.sqlite"
    graph.start(db, "t2", "risky change")
    done = graph.resume(db, "t2", approved=False)
    assert done["status"] == "done"
    assert "dev" not in done["steps"]


def test_state_survives_a_new_process(tmp_path):
    db = tmp_path / "runs.sqlite"
    graph.start(db, "t3", "x")
    # resume opens a fresh connection and checkpointer, as a new process would
    done = graph.resume(db, "t3", approved=True)
    assert done["steps"][-1] == "qa"
