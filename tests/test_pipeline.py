import json
import urllib.parse

from wf.integrations import JiraClient
from wf.pipeline import Pipeline

BODY = {
    "changelog": {
        "histories": [
            {
                "id": "9",
                "created": "2026-11-14T09:00:00.000+0000",
                "items": [{"field": "status", "fromString": "Ready for dev", "toString": "Dev in progress"}],
            }
        ]
    },
    "fields": {"labels": ["hotfix"]},
}


def make(tmp_path, clock=lambda: 0.0, search=lambda: []):
    return Pipeline(tmp_path / "wf.sqlite", fetch_changelog=lambda k: BODY, search_keys=search, clock=clock)


def test_webhook_routes_each_change_once(tmp_path):
    pipe = make(tmp_path)
    first = pipe.on_webhook("ENG-1")
    assert [d.action for d in first] == ["dispatch"]
    assert [d.owner for d in first] == ["oncall"]  # hotfix label on the ticket
    assert pipe.on_webhook("ENG-1") == []  # the same change again is not routed twice


def test_poll_skipped_while_webhooks_recent(tmp_path):
    pipe = make(tmp_path, clock=lambda: 1000.0, search=lambda: ["ENG-1"])
    pipe.on_webhook("ENG-1")
    assert pipe.poll(now=1000.0 + 60) == []


def test_poll_routes_when_quiet(tmp_path):
    pipe = make(tmp_path, clock=lambda: 0.0, search=lambda: ["ENG-1"])
    decisions = pipe.poll(now=5 * 60 * 60)
    assert [d.owner for d in decisions] == ["oncall"]


def test_jira_search_follows_next_page_token():
    pages = [
        {"issues": [{"key": "ENG-1"}, {"key": "ENG-2"}], "nextPageToken": "t2"},
        {"issues": [{"key": "ENG-3"}]},
    ]
    seen = []

    class Resp:
        def __init__(self, body):
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen.append(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query))
        return Resp(pages[len(seen) - 1])

    env = {"JIRA_BASE_URL": "https://x", "JIRA_EMAIL": "a", "JIRA_API_TOKEN": "t"}
    keys = JiraClient(env, opener).search_keys("updated >= -2h")
    assert keys == ["ENG-1", "ENG-2", "ENG-3"]
    assert "nextPageToken" not in seen[0] and seen[1]["nextPageToken"] == ["t2"]
    assert seen[0]["jql"] == ["updated >= -2h"]
