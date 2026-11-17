import hashlib
import hmac
import threading

from wf import github_webhook as gw

SECRET = "s3cret-for-tests"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_correct_signature_is_accepted():
    body = b'{"action": "completed"}'
    assert gw.signature_ok(SECRET, body, sign(body))


def test_a_wrong_or_missing_signature_is_refused():
    body = b'{"action": "completed"}'
    assert not gw.signature_ok(SECRET, body, sign(body, "other"))
    assert not gw.signature_ok(SECRET, body, None)
    assert not gw.signature_ok(SECRET, body + b" ", sign(body))  # the body is covered by the signature


def test_with_no_secret_configured_everything_is_refused():
    body = b"{}"
    assert not gw.signature_ok("", body, sign(body, ""))


def test_only_state_changing_events_wake_the_loop():
    assert gw.is_relevant("workflow_run") and gw.is_relevant("pull_request")
    assert not gw.is_relevant("star") and not gw.is_relevant("ping")


def test_the_route_refuses_a_bad_signature_and_wakes_on_a_good_one(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from wf import board, config as cfgmod
    from wf.web import agents_views
    from wf.web.app import create_app
    monkeypatch.setenv("WF_GITHUB_WEBHOOK_SECRET", SECRET)
    db = tmp_path / "wf.sqlite"; board.connect(db).close()
    cfg = tmp_path / "workflow.json"; cfgmod.save(cfgmod.Config(), cfg)
    client = TestClient(create_app(config_path=cfg, db_path=db, webhook_token="t"))
    body = json.dumps({"action": "completed"}).encode()
    class Spy(threading.Event):
        def __init__(self):
            super().__init__()
            self.sets = 0

        def set(self):
            self.sets += 1
            super().set()

    spy = Spy()
    monkeypatch.setattr(agents_views, "_wake", spy)  # the auto loop only reads the name, so the spy sees every set
    bad = client.post("/webhooks/github", content=body, headers={"X-GitHub-Event": "workflow_run",
                                                              "X-Hub-Signature-256": sign(body, "nope")})
    assert bad.status_code == 403 and spy.sets == 0
    good = client.post("/webhooks/github", content=body, headers={"X-GitHub-Event": "workflow_run",
                                                               "X-Hub-Signature-256": sign(body)})
    assert good.status_code == 200 and spy.sets == 1
