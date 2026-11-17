import pytest
from fastapi.testclient import TestClient

from wf import board, config as cfgmod
from wf.web.app import create_app
from tests.test_board import clock, payload, sync, ticket


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "wf.sqlite"
    c = board.connect(db)
    sync(c, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    c.close()
    cfg = tmp_path / "workflow.json"
    cfgmod.save(cfgmod.Config(), cfg)
    app = create_app(config_path=cfg, db_path=db, webhook_token="t")
    return TestClient(app)


def test_agents_page_renders(client):
    r = client.get("/agents")
    assert r.status_code == 200
    assert "Pipeline" in r.text and "ABC-1" in r.text
    assert "No repo linked" in r.text


def test_run_pass_without_repo_logs_and_redirects(client):
    r = client.post("/agents/tick", follow_redirects=False)
    assert r.status_code == 303
    assert "no repo is linked" in client.get("/agents/state").text


def test_publish_needs_confirm(client):
    r = client.post("/agents/publish/ABC-1", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "confirm box" in client.get("/agents/state").text


def test_state_partial_renders(client):
    r = client.get("/agents/state")
    assert r.status_code == 200 and "agents-state" in r.text


def test_ticket_timeline_shows_stages_and_next_role(client):
    r = client.get("/board/ticket/ABC-1")
    assert r.status_code == 200
    assert "Ready for dev" in r.text and "Run dev" not in r.text  # runs are automatic


def test_pause_button_pauses_and_resume_restores(client):
    assert "Pause work" in client.get("/agents").text
    r = client.post("/agents/pause", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/agents").text
    assert "Work is paused" in page and "Resume work" in page
    assert "work paused" in client.get("/agents/state").text

    client.post("/agents/resume", follow_redirects=False)
    page = client.get("/agents").text
    assert "Work is running" in page and "Pause work" in page


def test_a_paused_pass_starts_nothing(client):
    client.post("/agents/pause", follow_redirects=False)
    client.post("/agents/tick", follow_redirects=False)
    assert "work is paused" in client.get("/agents/state").text


def test_a_paused_ticket_run_starts_nothing(client):
    client.post("/agents/pause", follow_redirects=False)
    r = client.post("/agents/run/ABC-1", follow_redirects=False)
    assert r.status_code == 303
    assert "ABC-1: work is paused" in client.get("/agents/state").text
