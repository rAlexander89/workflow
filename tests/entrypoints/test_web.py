import pytest
from fastapi.testclient import TestClient

from wf.adapters.config import workspace as cfgmod
from wf.adapters.tracker.integrations import Check
from wf.entrypoints.web.app import create_app


class FakeJira:
    def check_board(self, link):
        return Check(False, "JIRA_API_TOKEN must be set")


@pytest.fixture
def client(tmp_path):
    app = create_app(
        config_path=tmp_path / "workflow.json",
        db_path=tmp_path / "wf.sqlite",
        jira=FakeJira(),
    )
    return TestClient(app, follow_redirects=False)


def test_root_redirects_to_runs(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/runs"


def test_links_page_empty(client):
    r = client.get("/links")
    assert r.status_code == 200
    assert "No repos linked yet." in r.text


def test_add_and_remove_repo(client, tmp_path):
    r = client.post("/links/repo", data={"name": "api", "repo_path": "/srv/wf/api.git"})
    assert r.status_code == 200 and "api" in r.text and "/srv/wf/api.git" in r.text
    assert cfgmod.load(tmp_path / "workflow.json").repos[0].path == "/srv/wf/api.git"

    r = client.post("/links/repo/remove", data={"name": "api"})
    assert "No repos linked yet." in r.text


def test_invalid_repo_shows_error_in_fragment(client):
    r = client.post("/links/repo", data={"name": "Bad Name", "repo_path": "/srv/x"})
    assert r.status_code == 200
    assert "lowercase" in r.text


def test_add_board(client):
    r = client.post("/links/board", data={"provider": "jira", "ref": "12", "label": "Platform"})
    assert "jira · 12" in r.text and "(Platform)" in r.text


def test_check_reports_each_link(client, tmp_path):
    client.post("/links/repo", data={"name": "api", "repo_path": str(tmp_path / "not-a-repo")})
    client.post("/links/board", data={"provider": "jira", "ref": "12"})
    r = client.post("/links/check")
    assert "api" in r.text
    assert "FAIL" in r.text and "JIRA_API_TOKEN must be set" in r.text


def test_start_run_redirects_to_detail_and_waits_for_approval(client):
    r = client.post("/runs", data={"request": "add search"})
    assert r.status_code == 303
    run_url = r.headers["location"]
    assert run_url.startswith("/runs/")

    page = client.get(run_url)
    assert page.status_code == 200
    assert "waiting_for_approval" in page.text
    assert "Approve" in page.text and "Reject" in page.text


def test_htmx_start_uses_hx_redirect(client):
    r = client.post("/runs", data={"request": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.headers["HX-Redirect"].startswith("/runs/")


def test_empty_request_is_rejected(client):
    assert client.post("/runs", data={"request": "   "}).status_code == 422


def test_approve_runs_dev_and_qa(client):
    run_url = client.post("/runs", data={"request": "add search"}).headers["location"]
    run_id = run_url.rsplit("/", 1)[1]
    r = client.post(f"/runs/{run_id}/decision", data={"approved": "yes"})
    assert r.status_code == 200
    assert "done" in r.text and "dev" in r.text and "qa" in r.text
    assert "Approve" not in r.text
    assert "done" in client.get("/runs").text


def test_reject_stops_before_dev(client):
    run_id = client.post("/runs", data={"request": "risky"}).headers["location"].rsplit("/", 1)[1]
    r = client.post(f"/runs/{run_id}/decision", data={"approved": "no"})
    assert "gate" in r.text and "dev" not in r.text.split("Steps")[1]


def test_decision_on_finished_run_is_a_no_op(client):
    run_id = client.post("/runs", data={"request": "x"}).headers["location"].rsplit("/", 1)[1]
    client.post(f"/runs/{run_id}/decision", data={"approved": "yes"})
    r = client.post(f"/runs/{run_id}/decision", data={"approved": "no"})
    assert "done" in r.text and "dev" in r.text  # still the approved run, not rewritten


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope").status_code == 404
    assert client.post("/runs/nope/decision", data={"approved": "yes"}).status_code == 404


def test_runs_list_shows_status(client):
    client.post("/runs", data={"request": "first request"})
    r = client.get("/runs")
    assert "first request" in r.text and "waiting_for_approval" in r.text



def test_a_tunneled_request_reaches_only_the_webhooks(client):
    assert client.get("/agents", headers={"x-forwarded-for": "203.0.113.5"}).status_code == 404
    assert client.get("/agents").status_code == 200


def test_a_jira_webhook_needs_the_token_and_wakes_the_sync(client, monkeypatch):
    from wf.entrypoints.web import agents_views
    monkeypatch.setenv("WF_JIRA_WEBHOOK_TOKEN", "tok123")
    assert client.post("/webhooks/jira/wrong", json={"webhookEvent": "jira:issue_updated"}).status_code == 403
    agents_views._jira_changed.clear()
    r = client.post("/webhooks/jira/tok123", json={"webhookEvent": "jira:issue_updated"})
    assert r.status_code == 200
    assert agents_views._jira_changed.is_set()
    agents_views._jira_changed.clear()
    client.post("/webhooks/jira/tok123", json={"webhookEvent": "comment_created"})
    assert not agents_views._jira_changed.is_set()
