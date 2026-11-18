import json
import subprocess
import urllib.error

from wf.adapters.config.workspace import BoardLink, RepoLink
from wf.adapters.tracker.integrations import JiraClient, check_repo


def fake_git(stdout="", returncode=0):
    calls = []

    def runner(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return runner, calls


def test_local_repo_check_uses_git_on_the_path():
    runner, calls = fake_git(stdout="true\n")
    res = check_repo(RepoLink("api", "/srv/wf/api.git"), runner=runner)
    assert res.ok and "bare repository" in res.detail
    assert calls[0] == ["git", "-C", "/srv/wf/api.git", "rev-parse", "--is-bare-repository"]


def test_local_repo_check_reports_a_missing_repo():
    runner, _ = fake_git(returncode=128)
    res = check_repo(RepoLink("api", "/nowhere"), runner=runner)
    assert not res.ok and "not a git repository" in res.detail


class FakeResp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_jira_board_ok_and_sends_basic_auth():
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResp({"name": "Platform board"})

    env = {"JIRA_BASE_URL": "https://example.atlassian.net/", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t"}
    res = JiraClient(env, opener).check_board(BoardLink("jira", "12"))
    assert res.ok and res.detail == "Platform board"
    assert seen["url"] == "https://example.atlassian.net/rest/agile/1.0/board/12"
    assert seen["auth"].startswith("Basic ")


def test_jira_missing_credentials_is_reported_not_raised():
    res = JiraClient({}, opener=None).check_board(BoardLink("jira", "12"))
    assert not res.ok and "JIRA_API_TOKEN" in res.detail


def test_jira_http_error_is_reported():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    env = {"JIRA_BASE_URL": "https://x", "JIRA_EMAIL": "a", "JIRA_API_TOKEN": "t"}
    res = JiraClient(env, opener).check_board(BoardLink("jira", "999"))
    assert not res.ok and "HTTP 404" in res.detail
