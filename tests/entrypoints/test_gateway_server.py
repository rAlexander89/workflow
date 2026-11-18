import io
import json

import pytest

from wf.entrypoints import gateway as gateway_server
from tests.builders import board_over, payload, sync, ticket


def rpc(gw, method, params=None, mid=1):
    return gw.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "wf.sqlite"
    with board_over(p) as b:
        sync(b.conn, payload(tickets=[ticket("ABC-1", status="Ready for dev")]))
    return p


def gw(db, role="dev", key="ABC-1", worktree=None, test_cmd=""):
    return gateway_server.Gateway(gateway_server.Scope({
        "WF_ROLE": role, "WF_KEY": key, "WF_DB": str(db),
        "WF_WORKTREE": str(worktree) if worktree else "", "WF_TEST_CMD": test_cmd}))


def call(g, name, args=None):
    r = rpc(g, "tools/call", {"name": name, "arguments": args or {}})
    return r["result"]


def test_scope_needs_role_and_key():
    with pytest.raises(ValueError):
        gateway_server.Scope({"WF_ROLE": "", "WF_KEY": "ABC-1"})


def test_initialize_and_list_tools(db):
    g = gw(db)
    init = rpc(g, "initialize")["result"]
    assert init["serverInfo"]["name"] == "wf-gateway"
    names = {t["name"] for t in rpc(g, "tools/list")["result"]["tools"]}
    assert {"get_ticket", "move_ticket", "flag_ticket", "run_tests", "commit", "open_pr"} <= names


def test_notifications_get_no_reply(db):
    assert gw(db).handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_get_ticket_is_scoped_to_the_run(db):
    res = call(gw(db), "get_ticket")
    assert res["isError"] is False
    assert json.loads(res["content"][0]["text"])["ticket"]["key"] == "ABC-1"


def test_move_is_checked_against_role(db):
    res = call(gw(db, role="qa"), "move_ticket", {"stage": "in_dev", "reason": "wrong role"})
    assert res["isError"] is True
    res = call(gw(db, role="dev"), "move_ticket", {"stage": "in_dev", "reason": "start"})
    assert res["isError"] is False


def test_open_pr_is_refused(db):
    res = call(gw(db), "open_pr")
    assert res["isError"] is True
    assert "may not open pull requests" in res["content"][0]["text"]


def test_unknown_tool_is_an_error_reply_not_a_crash(db):
    res = call(gw(db), "delete_everything")
    assert res["isError"] is True


def test_serve_round_trip(db):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    out = io.StringIO()
    gateway_server.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out,
                         scope=gateway_server.Scope({"WF_ROLE": "dev", "WF_KEY": "ABC-1", "WF_DB": str(db)}))
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2]


def test_post_comment_records_on_the_ticket(db):
    res = call(gw(db), "post_comment", {"body": "looks good so far"})
    assert res["isError"] is False
    conn = board_over(db).conn
    assert conn.execute("select count(*) from ticket_comment where ticket_key='ABC-1'").fetchone()[0] == 1


def test_request_changes_sends_ticket_back_to_dev(db):
    with board_over(db) as b:
        b.tickets.move_stage("ABC-1", "pr_open", "dev", "opened pr")
    res = call(gw(db, role="pr_review"), "request_changes", {"reason": "missing test"})
    assert res["isError"] is False
    conn = board_over(db).conn
    assert conn.execute("select stage from ticket where key='ABC-1'").fetchone()[0] == "in_dev"


def test_long_comments_are_refused_with_a_reason(db):
    res = call(gw(db), "post_comment", {"body": "x" * 700})
    assert res["isError"] is True
    assert "keep it under 600" in res["content"][0]["text"]


def app_gw(db, worktree, app_cmd):
    return gateway_server.Gateway(gateway_server.Scope({
        "WF_ROLE": "dev", "WF_KEY": "ABC-1", "WF_DB": str(db), "WF_WORKTREE": str(worktree),
        "WF_APP_CMD": app_cmd}))


def test_agent_can_start_fetch_and_stop_the_app(db, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>Login</h1>")
    g = app_gw(db, site, "python3 -m http.server {port} --bind 127.0.0.1")
    try:
        url = json.loads(call(g, "start_app")["content"][0]["text"])["url"]
        assert url.startswith("http://127.0.0.1:")
        out = json.loads(call(g, "fetch_local", {"path": "/index.html"})["content"][0]["text"])
        assert out["status"] == 200 and "<h1>Login</h1>" in out["body"]
    finally:
        call(g, "stop_app")
    assert call(g, "fetch_local", {"path": "/"})["isError"] is True


def test_start_app_needs_a_configured_command(db, tmp_path):
    res = call(app_gw(db, tmp_path, ""), "start_app")
    assert res["isError"] is True


def test_pr_review_will_not_approve_a_branch_with_stray_commits(db, tmp_path):
    import subprocess as sp
    from tests.builders import git, GIT_ENV
    repo = tmp_path / "r"; repo.mkdir()
    git(["init", "-q", "-b", "main"], repo)
    (repo / "a.txt").write_text("1"); git(["add", "-A"], repo); git(["commit", "-q", "-m", "base"], repo)
    git(["checkout", "-q", "-b", "agent/ABC-1"], repo)
    (repo / "a.txt").write_text("2"); git(["commit", "-qam", "unrelated listing change"], repo)
    git(["update-ref", "refs/remotes/origin/main", "main"], repo)
    g = gw(db, role="pr_review", worktree=repo)
    res = call(g, "approve_ticket")
    assert res["isError"] is True and "not approving" in res["content"][0]["text"]


@pytest.mark.parametrize("path", [
    "/register", "/register/", "/Register", "/register?next=/", "/signup", "/sign-up",
    "/%72egister", "/login/../register", "//register",
])
def test_is_signup_path_catches_sign_up_routes(path):
    assert gateway_server.is_signup_path(path) is True


@pytest.mark.parametrize("path", ["/", "/login", "/search", "/login?next=/register", "/registry"])
def test_is_signup_path_leaves_other_pages_alone(path):
    assert gateway_server.is_signup_path(path) is False


def test_fetch_local_refuses_the_sign_up_page(db, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>Login</h1>")
    g = app_gw(db, site, "python3 -m http.server {port} --bind 127.0.0.1")
    try:
        call(g, "start_app")
        res = call(g, "fetch_local", {"path": "/register"})
        assert res["isError"] is True
        assert "sign-up pages are not part of QA" in res["content"][0]["text"]
    finally:
        call(g, "stop_app")


def test_runner_headers_name_the_bot_and_send_the_secret_only_to_the_dev_host():
    dev = gateway_server.runner_headers("https://dev.example.com/login", "qa", "s3cret", "dev.example.com")
    assert dev == {"User-Agent": "wf-qa-bot/1.0", "X-Wf-Runner-Secret": "s3cret"}

    prod = gateway_server.runner_headers("https://example.com/login", "qa", "s3cret", "dev.example.com")
    assert prod == {"User-Agent": "wf-qa-bot/1.0"}

    local = gateway_server.runner_headers("http://127.0.0.1:3002/login", "qa", "s3cret", "dev.example.com")
    assert local == {"User-Agent": "wf-qa-bot/1.0"}

    no_secret = gateway_server.runner_headers("https://dev.example.com/login", "dev", "", "dev.example.com")
    assert no_secret == {"User-Agent": "wf-dev-bot/1.0"}

    no_host = gateway_server.runner_headers("https://dev.example.com/login", "dev", "s3cret", "")
    assert "X-Wf-Runner-Secret" not in no_host


def test_scope_reads_the_runner_secret_and_its_host_from_the_environment():
    s = gateway_server.Scope({"WF_ROLE": "qa", "WF_KEY": "ABC-1", "WF_RUNNER_SECRET": "x",
                              "WF_RUNNER_SECRET_HOST": "dev.example.com"})
    assert s.runner_secret == "x" and s.runner_secret_host == "dev.example.com"


def test_fetch_local_returns_a_whole_stylesheet_not_just_the_first_4000_bytes(db, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    css = "a{color:red}\n" * 1000 + "@media (max-width: 768px) { .site-title { display: block; } }\n"
    (site / "styles.css").write_text(css)
    g = app_gw(db, site, "python3 -m http.server {port} --bind 127.0.0.1")
    try:
        call(g, "start_app")
        out = json.loads(call(g, "fetch_local", {"path": "/styles.css"})["content"][0]["text"])
        assert out["status"] == 200 and "max-width: 768px" in out["body"]
    finally:
        call(g, "stop_app")
