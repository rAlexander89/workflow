import pytest
from fastapi.testclient import TestClient

from wf import board, config as cfgmod
from wf.entrypoints.web.app import create_app
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
    assert "ABC-1: Not started: work is paused" in client.get("/agents/state").text


def test_run_again_says_what_happened_on_the_ticket_page(client):
    client.post("/agents/pause", follow_redirects=False)
    r = client.post("/agents/run/ABC-1", follow_redirects=False)
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "Not started: work is paused" in page


def test_pages_have_no_polling_timers(client):
    ticket = client.get("/board/ticket/ABC-1").text
    assert 'data-key="ABC-1"' in ticket and "every " not in ticket
    board = client.get("/agents").text
    assert "every 4s" not in board and 'id="activity"' in board


def test_the_event_stream_opens_and_sends_the_board(client):
    with client.stream("GET", "/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data:"):
                assert "type" in line
                break

def test_a_card_that_changes_column_is_sent_as_a_move():
    from wf.entrypoints.web.agents_views import board_events
    prev = {"ABC-1": ("in_dev", "<article>a</article>")}
    now = {"ABC-1": ("pr_open", "<article>a</article>")}
    events = board_events(prev, {}, now, {})
    assert [e["type"] for e in events] == ["card.move"]
    assert events[0]["from"] == "in_dev" and events[0]["to"] == "pr_open"


def test_board_events_send_only_what_changed():
    from wf.entrypoints.web.agents_views import board_events
    prev = {"ABC-1": ("in_dev", "<a>1</a>"), "ABC-2": ("in_dev", "<a>2</a>"), "ABC-3": ("qa", "<a>3</a>")}
    now = {"ABC-1": ("in_dev", "<a>1 changed</a>"), "ABC-2": ("in_dev", "<a>2</a>"), "ABC-4": ("qa", "<a>4</a>")}
    kinds = sorted((e["type"], e["key"]) for e in board_events(prev, {}, now, {}))
    assert kinds == [("card.add", "ABC-4"), ("card.remove", "ABC-3"), ("card.update", "ABC-1")]


def test_a_flagged_ticket_shows_its_flag_and_the_reason_on_the_page(tmp_path):
    db = tmp_path / "wf.sqlite"
    c = board.connect(db)
    sync(c, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    board.set_flag(c, "ABC-1", "needs-human", "qa", "the acceptance criteria are unclear")
    c.close()
    cfg = tmp_path / "workflow.json"
    cfgmod.save(cfgmod.Config(), cfg)
    page = TestClient(create_app(config_path=cfg, db_path=db, webhook_token="t")).get("/agents")
    assert page.status_code == 200
    assert "needs-human" in page.text and "the acceptance criteria are unclear" in page.text
