import http.server
import json
import threading

import pytest

from wf import agents, gateway_server

PASSWORD = "Sup3r-secret!pw"


class FakeAuthAPI(http.server.BaseHTTPRequestHandler):
    sessions = set()

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code == 200 and "email" in body:
            self.send_header("Set-Cookie", "sid=ok; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/auth/login":
            if body.get("email") == "qa@fokist.com" and body.get("password") == PASSWORD:
                FakeAuthAPI.sessions.add("sid")
                return self._send(200, {"email": body["email"]})
            return self._send(401, {"error": "bad"})
        if self.path == "/api/auth/logout":
            FakeAuthAPI.sessions.clear()
            return self._send(200, {"ok": True})
        self._send(404, {})

    def do_GET(self):
        if self.path == "/api/auth/me" and "sid" in FakeAuthAPI.sessions:
            return self._send(200, {"email": "qa@fokist.com"})
        self._send(401, {"error": "no session"})

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


def gw(api, creds, role="qa", email="qa@fokist.com"):
    return gateway_server.Gateway(gateway_server.Scope({
        "WF_ROLE": role, "WF_KEY": "ABC-1", "WF_DB": "x.sqlite", "WF_BOT_EMAIL": email,
        "WF_BOT_CREDENTIALS": str(creds), "WF_BOT_API": api, "WF_BOT_HOST": "localhost:3001"}))


def test_bot_logs_in_and_the_password_is_never_returned(api, creds):
    g = gw(api, creds)
    out = g.bot_login({})
    assert out == {"logged_in": True, "email": "qa@fokist.com"}
    assert PASSWORD not in json.dumps(out)
    assert g.bot_logout({})["logged_out"] is True


def test_wrong_bot_password_is_refused_without_leaking_it(api, creds):
    g = gw(api, creds, role="dev", email="dev@fokist.com")
    with pytest.raises(PermissionError, match="HTTP 401"):
        g.bot_login({})


def test_a_run_without_a_bot_account_cannot_log_in(api, creds):
    g = gateway_server.Gateway(gateway_server.Scope({"WF_ROLE": "qa", "WF_KEY": "ABC-1", "WF_DB": "x.sqlite"}))
    with pytest.raises(ValueError, match="no bot account"):
        g.bot_login({})


def test_system_prompt_names_the_account_and_file_but_never_the_password(tmp_path):
    text = agents.system_prompt("qa", "qa@fokist.com", "/home/x/.config/fokist-bots/credentials.env")
    assert "qa@fokist.com" in text and "credentials.env" in text
    assert "bot_login" in text and PASSWORD not in text
    assert "##" in text  # the identity block is its own section
    assert "qa@fokist.com" not in agents.system_prompt("qa")  # no block without a bot account
