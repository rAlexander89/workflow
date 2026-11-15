import json
import shutil
import stat
import subprocess

import pytest

from wf import claude_run, cli
from wf import config as cfgmod
from wf.config import Config, ModelConfig
from wf.model import Message, ModelError, ModelRequest, ProviderNotAllowed, build_model, known_providers
from wf.model.claude_code import ClaudeCodeModel

REQ = ModelRequest(system="be brief", messages=(Message("user", "hi"),), max_tokens=64)
FAKE_BIN = lambda name: "/usr/local/bin/claude"  # noqa: E731
NO_BIN = lambda name: None  # noqa: E731


def _cfg(access=True):
    return ModelConfig(provider="claude-code", model="claude-sonnet-5-5", claude_code_access=access)


def _proc(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- the claude-code provider -------------------------------------------------

def test_claude_code_is_registered_and_off_by_default():
    assert known_providers() == ["anthropic-api", "claude-code", "ollama"]
    assert ModelConfig().claude_code_access is False
    with pytest.raises(ProviderNotAllowed, match="not enabled"):
        build_model(_cfg(access=False), env={})


def test_claude_code_needs_the_cli_on_path():
    with pytest.raises(ModelError, match="not on PATH"):
        ClaudeCodeModel(_cfg(), env={}, which=NO_BIN)


def test_claude_code_completion_uses_no_tools_and_reads_result():
    seen = {}

    def fake_run(argv, timeout):
        seen["argv"], seen["timeout"] = argv, timeout
        return _proc(json.dumps({
            "type": "result", "is_error": False, "result": "pong",
            "usage": {"input_tokens": 12, "output_tokens": 3,
                      "cache_read_input_tokens": 40, "cache_creation_input_tokens": 5},
        }))

    model = ClaudeCodeModel(_cfg(), env={"ANTHROPIC_API_KEY": "sk-should-not-be-used"},
                            runner=fake_run, which=FAKE_BIN)
    resp = model.complete(REQ)

    argv = seen["argv"]
    assert argv[:3] == ["/usr/local/bin/claude", "-p", "hi"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tools for a plain completion
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "be brief"
    assert not any("sk-should-not-be-used" in a for a in argv)
    assert resp.text == "pong"
    assert (resp.input_tokens, resp.output_tokens) == (12, 3)
    assert (resp.cache_read_tokens, resp.cache_write_tokens) == (40, 5)


def test_claude_code_failure_is_an_error_with_short_detail():
    def fake_run(argv, timeout):
        return _proc(json.dumps({"is_error": True, "result": "not logged in"}), returncode=1)

    model = ClaudeCodeModel(_cfg(), env={}, runner=fake_run, which=FAKE_BIN)
    with pytest.raises(ModelError, match="not logged in"):
        model.complete(REQ)


def test_claude_code_rejects_non_json_and_timeouts():
    model = ClaudeCodeModel(_cfg(), env={}, runner=lambda a, t: _proc("not json"), which=FAKE_BIN)
    with pytest.raises(ModelError, match="non-JSON"):
        model.complete(REQ)

    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    model = ClaudeCodeModel(_cfg(), env={}, runner=slow, which=FAKE_BIN)
    with pytest.raises(ModelError, match="timed out"):
        model.complete(REQ)


# --- the run component ---------------------------------------------------------

def _worktree(tmp_path):
    wt = tmp_path / "PROJ-1-widget"
    wt.mkdir(exist_ok=True)
    (wt / ".git").write_text("gitdir: /elsewhere\n")  # a worktree has a .git file
    return wt


def _run(tmp_path, runner, access=True, **kw):
    args = dict(worktree=_worktree(tmp_path), prompt="Ticket PROJ-1", system_prompt="dev rules",
                model="claude-sonnet-5-5", run_dir=tmp_path / "runs" / "r1", profile="dev",
                gateway_command=["python", "-m", "gateway"], access=access, claude_bin="/usr/local/bin/claude",
                runner=runner)
    args.update(kw)
    return claude_run.run(**args)


def test_run_refuses_without_access_and_never_starts_claude(tmp_path):
    called = []
    with pytest.raises(ProviderNotAllowed, match="not enabled"):
        _run(tmp_path, runner=lambda *a: called.append(a), access=False)
    assert called == []


def test_run_requires_a_real_worktree(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a git worktree"):
        _run(tmp_path, runner=lambda *a: _proc(), worktree=plain)


def test_run_command_limits_tools_and_connects_only_the_gateway(tmp_path):
    seen = {}

    def fake(argv, cwd, timeout):
        seen.update(argv=argv, cwd=cwd, timeout=timeout)
        return _proc("")

    wt = _worktree(tmp_path)
    result = _run(tmp_path, runner=fake, worktree=wt)
    argv = seen["argv"]

    assert argv[:3] == ["/usr/local/bin/claude", "-p", "Ticket PROJ-1"]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--add-dir") + 1] == str(wt)
    assert seen["cwd"] == wt
    builtin = argv[argv.index("--tools") + 1: argv.index("--allowedTools")]
    assert builtin == ["Read", "Edit", "Write"]  # no Bash
    allowed = argv[argv.index("--allowedTools") + 1: argv.index("--add-dir")]
    assert all(t.startswith("mcp__gateway__") or t in {"Read", "Edit", "Write"} for t in allowed)
    assert "Bash" not in allowed

    mcp = result.output_path.parent / "gateway.json"
    assert json.loads(mcp.read_text()) == {
        "mcpServers": {"gateway": {"command": "python", "args": ["-m", "gateway"]}}}
    assert stat.S_IMODE(mcp.stat().st_mode) == 0o600


def test_run_saves_output_and_reads_the_final_result(tmp_path):
    lines = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": "working"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "PR opened"}),
    ])
    result = _run(tmp_path, runner=lambda argv, cwd, t: _proc(lines))
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.final["result"] == "PR opened"
    assert result.output_path.read_text() == lines


def test_run_time_limit_stops_the_run_and_keeps_partial_output(tmp_path):
    def slow(argv, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, output=b'{"type": "assistant"}\n')

    result = _run(tmp_path, runner=slow, timeout=5)
    assert result.timed_out is True and result.exit_code is None
    assert "assistant" in result.output_path.read_text()
    assert result.final is None


# --- config and CLI --------------------------------------------------------------

def test_access_flag_round_trips_and_old_configs_default_to_off(tmp_path):
    cfg = Config(model=ModelConfig(provider="claude-code", model="m", claude_code_access=True))
    restored = Config.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert restored.model.claude_code_access is True
    assert Config.from_dict({"repos": [], "boards": []}).model.claude_code_access is False


def test_cli_enable_and_status(tmp_path, capsys):
    path = tmp_path / "workflow.json"
    assert cli.main(["--config", str(path), "model", "set", "--provider", "claude-code",
                     "--model", "claude-sonnet-5-5", "--enable-claude-code"]) == 0
    assert cfgmod.load(path).model.claude_code_access is True
    capsys.readouterr()
    cli.main(["--config", str(path), "model", "status"])
    out = capsys.readouterr().out
    assert "enabled on this machine" in out
    assert "Claude Code login" in out

    cli.main(["--config", str(path), "model", "set", "--disable-claude-code"])
    assert cfgmod.load(path).model.claude_code_access is False


def test_cli_run_claude_refuses_when_not_enabled(tmp_path, capsys):
    path = tmp_path / "workflow.json"
    cli.main(["--config", str(path), "model", "set", "--provider", "claude-code"])
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    code = cli.main(["--config", str(path), "run-claude", "--worktree", str(_worktree(tmp_path)),
                     "--prompt-file", str(prompt), "--system-prompt-file", str(prompt),
                     "--run-dir", str(tmp_path / "r"), "--gateway-cmd", "python -m gateway"])
    assert code == 2
    assert "not enabled" in capsys.readouterr().err


def test_cli_run_claude_runs_when_enabled(tmp_path, capsys, monkeypatch):
    path = tmp_path / "workflow.json"
    cli.main(["--config", str(path), "model", "set", "--provider", "claude-code",
              "--model", "claude-sonnet-5-5", "--enable-claude-code"])
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    monkeypatch.setattr(claude_run, "default_runner",
                        lambda argv, cwd, t: _proc(json.dumps({"type": "result", "result": "done"})))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    code = cli.main(["--config", str(path), "run-claude", "--worktree", str(_worktree(tmp_path)),
                     "--prompt-file", str(prompt), "--system-prompt-file", str(prompt),
                     "--run-dir", str(tmp_path / "r"), "--gateway-cmd", "python -m gateway"])
    assert code == 0
    assert "result: done" in capsys.readouterr().out
