import json
import subprocess

from wf.adapters.forge import forge as forge

URL = "https://git.example.com/acme/app/-/merge_requests/7"


def glab(routes, calls):
    """A fake `glab api`: routes maps a path fragment to the JSON it returns."""
    def run(argv, cwd):
        calls.append(argv)
        path = next(a for a in argv if a.startswith("/projects"))
        for frag, body in routes.items():
            if frag in path:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
    return run


def test_gitlab_mr_open_with_jobs_running_is_pending():
    calls = []
    routes = {
        "/merge_requests/7/commits": [{"title": "ABC-29: fix"}],
        "/merge_requests/7": {"state": "opened", "sha": "abc", "head_pipeline": {"id": 9}, "detailed_merge_status": "mergeable"},
        "/pipelines/9/jobs": [{"status": "running"}],
    }
    st = forge.GitLab(host="git.example.com", runner=glab(routes, calls)).pr_status(URL)
    assert st["ok"] and st["state"] == "OPEN" and not st["merged"] and st["checks"] == "pending"
    assert st["commit_subjects"] == ["ABC-29: fix"]


def test_gitlab_merged_mr_reports_merged(tmp_path):
    routes = {"/merge_requests/7/commits": [], "/merge_requests/7": {"state": "merged", "sha": "abc", "head_pipeline": None}}
    st = forge.GitLab(host="git.example.com", runner=glab(routes, [])).pr_status(URL)
    assert st["merged"] and st["state"] == "MERGED"


def test_gitlab_merge_request_is_opened_and_merged():
    calls = []
    routes = {"/merge_requests": {"web_url": "https://git.example.com/acme/app/-/merge_requests/8"}}
    g = forge.GitLab(host="git.example.com", runner=glab(routes, calls))
    assert g.open_pr("acme/app", "agent/ABC-29", "ABC-29: x", "body").endswith("/merge_requests/8")
    assert any("-X" in c and "POST" in c for c in calls)


def test_a_gitlab_link_is_routed_to_gitlab_and_a_github_link_to_github():
    assert forge.for_url(URL).name == "gitlab"
    assert forge.for_url("https://github.com/o/r/pull/1").name == "github"


def test_check_states_for_both_forges():
    assert forge.checks_state([("COMPLETED", "SUCCESS")]) == "passed"
    assert forge.checks_state([("COMPLETED", "FAILURE")]) == "failed"
    assert forge.checks_state([("IN_PROGRESS", "")]) == "pending"
    assert forge.checks_state([]) == "none"
