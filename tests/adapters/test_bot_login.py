import http.server
import json
import threading
import urllib.parse

import pytest

from pathlib import Path

from wf.adapters.runner.prompts import FilePromptLibrary
from wf.domain.execution.agent_run import compose_system_prompt
from wf.entrypoints import gateway as gateway_server

PASSWORD = "Sup3r-secret!pw"


class FakeAuthAPI(http.server.BaseHTTPRequestHandler):
    """Acts like the Example HTML login: a form POST sets a session cookie and redirects."""
    sessions = set()

    def _reply(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _has_session(self):
        return "sid=ok" in (self.headers.get("Cookie") or "") and "sid" in FakeAuthAPI.sessions

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if self.path == "/login":
            ok = form.get("email") == ["qa@example.com"] and form.get("password") == [PASSWORD]
            if ok:
                FakeAuthAPI.sessions.add("sid")
                return self._reply(303, headers=[("Location", "/search"), ("Set-Cookie", "sid=ok; Path=/")])
            return self._reply(200, b"<form>login</form>")
        if self.path == "/logout":
            FakeAuthAPI.sessions.clear()
            return self._reply(303, headers=[("Location", "/login")])
        self._reply(404)

    def do_GET(self):
        if self.path == "/search" and self._has_session():
            return self._reply(200, b"<h1>search</h1>")
        self._reply(303, headers=[("Location", "/login")])

    def log_message(self, *a):
        pass


@pytest.fixture
def api():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAuthAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeAuthAPI.sessions.clear()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def creds(tmp_path):
    p = tmp_path / "credentials.env"
    p.write_text(f"qa_PASSWORD={PASSWORD}\ndev_PASSWORD=other-pw-1!\n")
    return p


def gw(api, creds, role="qa", email="qa@example.com"):
    return gateway_server.Gateway(gateway_server.Scope({
        "WF_ROLE": role, "WF_KEY": "ABC-1", "WF_DB": "x.sqlite", "WF_BOT_EMAIL": email,
        "WF_BOT_CREDENTIALS": str(creds), "WF_BOT_APP": api}))


def test_bot_logs_in_and_the_password_is_never_returned(api, creds):
    g = gw(api, creds)
    out = g.bot_login({})
    assert out == {"logged_in": True, "email": "qa@example.com"}
    assert PASSWORD not in json.dumps(out)
    assert g.bot_logout({})["logged_out"] is True


def test_wrong_bot_password_is_refused_without_leaking_it(api, creds):
    g = gw(api, creds, role="dev", email="dev@example.com")
    with pytest.raises(PermissionError, match="login failed"):
        g.bot_login({})


def test_a_run_without_a_bot_account_cannot_log_in(api, creds):
    g = gateway_server.Gateway(gateway_server.Scope({"WF_ROLE": "qa", "WF_KEY": "ABC-1", "WF_DB": "x.sqlite"}))
    with pytest.raises(ValueError, match="no bot account"):
        g.bot_login({})


def system_prompt(role, bot_email="", credentials_path=""):
    prompts = FilePromptLibrary(Path(__file__).resolve().parents[2] / "prompts")
    return compose_system_prompt(prompts.role_prompt(role), bot_email, credentials_path)


def test_system_prompt_names_the_account_and_file_but_never_the_password(tmp_path):
    text = system_prompt("qa", "qa@example.com", "/home/x/.config/example-bots/credentials.env")
    assert "qa@example.com" in text and "credentials.env" in text
    assert "bot_login" in text and PASSWORD not in text
    assert "##" in text  # the identity block is its own section
    assert "qa@example.com" not in system_prompt("qa")  # no block without a bot account
