import pytest
from fastapi.testclient import TestClient

from wf.events import (
    ChangeStore,
    Intake,
    StatusChange,
    issue_key_from_webhook,
    parse_changelog,
    poll_due,
)
from wf.integrations import Check
from wf.web.app import create_app

BODY = {
    "key": "ENG-7",
    "changelog": {
        "histories": [
            {
                "id": "1001",
                "created": "2026-11-14T09:00:00.000+0000",
                "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
            },
            {
                "id": "1002",
                "created": "2026-11-14T10:00:00.000+0000",
                "items": [
                    {"field": "assignee", "fromString": None, "toString": "bot"},
                    {"field": "status", "fromString": "In Progress", "toString": "PR open"},
                ],
            },
        ]
    },
}


def test_parse_keeps_only_status_items_with_history_ids():
    changes = parse_changelog("ENG-7", BODY)
    assert [(c.history_id, c.from_status, c.to_status) for c in changes] == [
        ("1001", "To Do", "In Progress"),
        ("1002", "In Progress", "PR open"),
    ]


def test_store_records_each_change_once(tmp_path):
    store = ChangeStore(tmp_path / "wf.sqlite")
    changes = parse_changelog("ENG-7", BODY)
    assert len(store.record_new(changes)) == 2
    assert store.record_new(changes) == []


def test_webhook_and_poll_for_same_change_count_once(tmp_path):
    store = ChangeStore(tmp_path / "wf.sqlite")
    intake = Intake(store, lambda key: BODY)
    assert len(intake.handle("ENG-7")) == 2  # the webhook trigger
    # the hourly poll reads the same issue again later
    store.mark_webhook(0)
    assert len(intake.poll(lambda: ["ENG-7"], now=10 * 60 * 60)) == 0


def test_poll_runs_only_after_two_quiet_hours():
    hour = 60 * 60
    assert poll_due(None, now=0)
    assert not poll_due(0, now=hour)
    assert poll_due(0, now=2 * hour)


def test_poll_skipped_when_webhook_recent(tmp_path):
    store = ChangeStore(tmp_path / "wf.sqlite", clock=lambda: 1000.0)
    store.mark_webhook(1000.0)
    intake = Intake(store, lambda key: BODY)
    assert intake.poll(lambda: ["ENG-7"], now=1000.0 + 60) == []


def test_poll_fetches_when_quiet(tmp_path):
    store = ChangeStore(tmp_path / "wf.sqlite", clock=lambda: 0.0)
    intake = Intake(store, lambda key: BODY)
    found = intake.poll(lambda: ["ENG-7"], now=5 * 60 * 60)
    assert len(found) == 2


def test_issue_key_from_webhook():
    assert issue_key_from_webhook({"issue": {"key": "ENG-7"}}) == "ENG-7"
    assert issue_key_from_webhook({}) is None


class FakeJira:
    def check_board(self, link):
        return Check(True, "ok")

    def get_issue_changelog(self, key):
        return BODY


@pytest.fixture
def client(tmp_path):
    app = create_app(
        config_path=tmp_path / "workflow.json",
        db_path=tmp_path / "wf.sqlite",
        jira=FakeJira(),
        webhook_token="s3cret",
    )
    return TestClient(app)


def webhook_payload():
    return {"issue": {"key": "ENG-7"}, "changelog": {"histories": []}}


def test_webhook_refused_without_token(client):
    assert client.post("/webhooks/jira", json=webhook_payload()).status_code == 403
    assert client.post("/webhooks/jira?token_param=wrong", json=webhook_payload()).status_code == 403


def test_webhook_fails_closed_when_no_token_configured(tmp_path):
    app = create_app(
        config_path=tmp_path / "workflow.json",
        db_path=tmp_path / "wf.sqlite",
        jira=FakeJira(),
        webhook_token="",
    )
    c = TestClient(app)
    assert c.post("/webhooks/jira?token_param=", json=webhook_payload()).status_code == 403


def test_webhook_records_changes_from_rest_not_payload(client):
    r = client.post("/webhooks/jira?token_param=s3cret", json=webhook_payload())
    assert r.status_code == 200 and r.json()["routed"] == 2
    r = client.post("/webhooks/jira?token_param=s3cret", json=webhook_payload())
    assert r.json()["routed"] == 0
