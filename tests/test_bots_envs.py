import stat
import subprocess
import sys

from wf import bots, envs


def test_credentials_are_written_once_with_owner_only_permissions(tmp_path):
    path = tmp_path / "creds.env"
    accounts = {r: {"email": bots.account_email(r, "example.com")} for r in ("dev", "qa")}
    assert bots.write_credentials(path, accounts) == ["dev", "qa"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_text()
    assert "DEV_EMAIL=dev@example.com" in first and "QA_PASSWORD=" in first
    assert bots.write_credentials(path, accounts) == []  # existing passwords are kept
    assert path.read_text() == first


def test_setup_declined_writes_nothing(tmp_path):
    said = []
    out = bots.setup_interactive("example.com", ask=lambda q: "n", say=said.append, path=tmp_path / "c.env")
    assert out is None and not (tmp_path / "c.env").exists()
    text = "\n".join(said)
    assert "Bots may:" in text and "Bots may not:" in text


def test_setup_accepted_writes_credentials_and_returns_config(tmp_path):
    out = bots.setup_interactive("example.com", ask=lambda q: "y", say=lambda m: None, path=tmp_path / "c.env")
    assert out["enabled"] and out["domain"] == "example.com"
    text = (tmp_path / "c.env").read_text()
    assert "STAFF_EMAIL=staff@example.com" in text and "_PASSWORD=" in text


def test_passwords_are_never_printed(tmp_path):
    said = []
    bots.setup_interactive("example.com", ask=lambda q: "y", say=said.append, path=tmp_path / "c.env")
    secret = [l.split("=", 1)[1] for l in (tmp_path / "c.env").read_text().splitlines() if "_PASSWORD=" in l]
    assert secret and not any(v in m for m in said for v in secret)


SERVER = ("{py} -m http.server {{port}} --bind 127.0.0.1".format(py=sys.executable))


def test_env_up_and_down(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "index.html").write_text("<h1>ok</h1>")
    state = tmp_path / "state"
    st = envs.up("demo", repo=repo, app_cmd=SERVER, state_dir=state, timeout=30)
    try:
        assert st["running"] and st["url"].startswith("http://127.0.0.1:")
        assert envs.status(state, "demo")["running"]
    finally:
        assert envs.down("demo", state_dir=state)
    assert envs.status(state, "demo") is None


def test_env_up_needs_a_command(tmp_path):
    try:
        envs.up("demo", repo=tmp_path, app_cmd="", state_dir=tmp_path)
    except ValueError as e:
        assert "agents.app_cmd" in str(e)
    else:
        raise AssertionError("expected ValueError")
