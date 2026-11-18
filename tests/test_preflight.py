import subprocess
from pathlib import Path

from wf import claude_run, preflight

from wf.entrypoints import cli
from wf.model import claude_code
from wf.model.claude_code import subscription_env

FOUND = lambda name: f"/usr/local/bin/{name}"  # noqa: E731
MISSING = lambda name: None  # noqa: E731
GOOD_LOGIN = {"loggedIn": True, "authMethod": "claude.ai"}


def _by_name(checks):
    return {c.name: c for c in checks}


def _ready(**kw):
    args = dict(system="darwin", machine="arm64", python_version=(3, 14), which=FOUND,
                auth_status=lambda b, e: GOOD_LOGIN, environ={}, claude_code_access=True)
    args.update(kw)
    return _by_name(preflight.run_checks(**args))


def test_subscription_env_removes_key_variables_without_touching_the_original():
    original = {"ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t", "HOME": "/home/user"}
    env = subscription_env(original)
    assert env == {"HOME": "/home/user"}
    assert "ANTHROPIC_API_KEY" in original  # the caller's copy is unchanged


def test_runs_and_completions_do_not_inherit_the_key(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(kw["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_run.subprocess, "run", fake_run)
    claude_code.default_runner(["claude"], 5)
    claude_run.default_runner(["claude"], Path("."), 5)
    assert all("ANTHROPIC_API_KEY" not in env for env in seen)


def test_ready_mac_passes_every_required_check():
    checks = _ready()
    assert all(c.status == "ok" for c in checks.values()), checks
    assert checks["claude login"].detail == "signed in (claude.ai)"


def test_intel_or_linux_fails_the_mac_check():
    checks = _ready(system="linux", machine="x86_64")
    assert checks["apple silicon mac"].failed


def test_old_python_fails():
    assert _ready(python_version=(3, 10))["python 3.11+"].failed


def test_missing_claude_fails_and_skips_the_login_check():
    checks = _ready(which=lambda n: None if n == "claude" else FOUND(n))
    assert checks["claude cli"].failed
    assert "claude login" not in checks


def test_api_key_login_fails_even_though_runs_would_strip_the_key():
    checks = _ready(auth_status=lambda b, e: {"loggedIn": True, "authMethod": "api_key",
                                              "apiKeySource": "ANTHROPIC_API_KEY"})
    assert checks["claude login"].failed
    assert "API key" in checks["claude login"].detail


def test_logged_out_fails_with_the_fix():
    checks = _ready(auth_status=lambda b, e: {"loggedIn": False})
    assert checks["claude login"].failed
    assert "claude auth login" in checks["claude login"].detail


def test_key_variables_warn_and_are_hidden_from_the_auth_check():
    seen_env = {}

    def auth(binary, env):
        seen_env.update(env)
        return GOOD_LOGIN

    checks = _ready(environ={"ANTHROPIC_API_KEY": "sk-x", "PATH": "/bin"}, auth_status=auth)
    assert checks["api key variables"].status == "warn"
    assert "ANTHROPIC_API_KEY" not in seen_env
    assert checks["claude login"].status == "ok"


def test_access_not_enabled_fails():
    assert _ready(claude_code_access=False)["claude code access"].failed


def test_doctor_exit_code_follows_required_checks(tmp_path, monkeypatch, capsys):
    path = tmp_path / "workflow.json"
    monkeypatch.setattr(preflight, "run_checks",
                        lambda **kw: [preflight.Check("claude cli", "fail", "missing")])
    assert cli.main(["--config", str(path), "doctor"]) == 1
    assert "[FAIL] claude cli" in capsys.readouterr().out

    monkeypatch.setattr(preflight, "run_checks",
                        lambda **kw: [preflight.Check("claude cli", "ok", "/x"),
                                      preflight.Check("api key variables", "warn", "set")])
    assert cli.main(["--config", str(path), "doctor"]) == 0
