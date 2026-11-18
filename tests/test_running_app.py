import http.server
import threading

import pytest

from wf import envs
from wf.entrypoints import gateway as gateway_server


@pytest.fixture
def running_app():
    """An app that is already running on a local port, like the one the user keeps up."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<h1>Example</h1>" if self.path != "/missing" else b"no"
            self.send_response(200 if self.path != "/missing" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_env_up_reuses_a_running_app_without_starting_one(tmp_path, running_app):
    st = envs.up("app", repo=tmp_path, app_cmd="false", state_dir=tmp_path, app_url=running_app)
    assert st["external"] and st["running"] and st["url"] == running_app


def test_env_down_forgets_a_borrowed_app_but_leaves_it_running(tmp_path, running_app):
    envs.up("app", repo=tmp_path, app_cmd="", state_dir=tmp_path, app_url=running_app)
    assert envs.down("app", state_dir=tmp_path)
    assert envs.answers(running_app)  # still up
    assert envs.status(tmp_path, "app") is None


def test_env_up_without_a_running_app_or_command_says_so(tmp_path):
    with pytest.raises(ValueError, match="no app is running"):
        envs.up("app", repo=tmp_path, app_cmd="", state_dir=tmp_path, app_url="http://127.0.0.1:1")


def test_agent_gateway_uses_the_running_app(tmp_path, running_app):
    scope = gateway_server.Scope({"WF_ROLE": "qa", "WF_KEY": "ABC-1", "WF_DB": str(tmp_path / "w.sqlite"),
                                  "WF_APP_URL": running_app})
    g = gateway_server.Gateway(scope)
    out = g.start_app({})
    assert out["url"] == running_app
    page = g.fetch_local({"path": "/"})
    assert page["status"] == 200 and "Example" in page["body"]
    g.stop_app({})
    assert envs.answers(running_app)  # the agent never stops the app it borrowed
