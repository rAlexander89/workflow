"""One agent run through Claude Code: `claude -p` started in a ticket's worktree.

The harness creates the worktree and the run directory first. This module builds the
command, writes the gateway's MCP config, starts the process with a time limit, and
saves the streamed output. It refuses to run unless `claude_code_access` is enabled.

Profiles set what the run may do. Built-in tools are limited to the worktree's file
tools. Shell access is left out, so tests run through a gateway tool that executes
them in the run container. The gateway tool names are placeholders until the gateway
server exists, and they must match its server name.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from wf.domain.execution.agent_run import AgentRunRequest, RunResult  # noqa: F401
from wf.model import ProviderNotAllowed
from wf.model.claude_code import CLAUDE_BIN, subscription_env

GATEWAY_SERVER = "gateway"

def _tools(*names: str) -> list[str]:
    return [f"mcp__{GATEWAY_SERVER}__{n}" for n in names]


# Each role gets only the gateway tools its job needs. Only dev may edit files.
PROFILES: dict[str, dict[str, list[str]]] = {
    "dev": {
        "builtin_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
        "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"] + _tools(
            "get_ticket", "move_ticket", "flag_ticket", "run_tests", "commit", "post_comment",
            "start_app", "fetch_local", "stop_app", "request_perf_check"),
    },
    "perf_review": {
        "builtin_tools": ["Read", "Glob", "Grep"],
        "allowed_tools": ["Read", "Glob", "Grep"] + _tools(
            "get_ticket", "flag_ticket", "run_tests", "approve_ticket", "post_comment", "request_changes"),
    },
    "pr_review": {
        "builtin_tools": ["Read", "Glob", "Grep"],
        "allowed_tools": ["Read", "Glob", "Grep"] + _tools(
            "get_ticket", "flag_ticket", "run_tests", "approve_ticket", "post_comment", "request_changes"),
    },
    "qa": {
        "builtin_tools": ["Read", "Glob", "Grep"],
        "allowed_tools": ["Read", "Glob", "Grep"] + _tools(
            "get_ticket", "move_ticket", "flag_ticket", "run_tests", "approve_ticket", "post_comment",
            "start_app", "fetch_local", "stop_app"),
    },
    "product": {
        "builtin_tools": ["Read", "Glob", "Grep"],
        "allowed_tools": ["Read", "Glob", "Grep"] + _tools("get_ticket", "move_ticket", "post_comment", "flag_ticket"),
    },
    "staff": {
        "builtin_tools": ["Read", "Glob", "Grep"],
        "allowed_tools": ["Read", "Glob", "Grep"] + _tools(
            "get_ticket", "move_ticket", "post_comment", "flag_ticket", "link_blocking", "set_order"),
    },
    "performance": {
        "builtin_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
        "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"] + _tools(
            "get_ticket", "run_tests", "commit", "post_comment", "flag_ticket", "approve_ticket",
            "start_app", "fetch_local", "stop_app"),
    },
}

# Every role may sign in to the local app as its own bot account and sign out again.
for _spec in PROFILES.values():
    _spec["allowed_tools"] += _tools("bot_login", "bot_logout")

# (argv, cwd, timeout seconds) -> CompletedProcess. Injected so tests run offline.
Runner = Callable[[list[str], Path, float], "subprocess.CompletedProcess[str]"]


def default_runner(argv: list[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
                          env=subscription_env())


def build_argv(
    *,
    claude_bin: str,
    prompt: str,
    system_prompt: str,
    model: str,
    mcp_config: Path,
    builtin_tools: list[str],
    allowed_tools: list[str],
    add_dir: Path,
) -> list[str]:
    return [
        claude_bin, "-p", prompt,
        "--model", model,
        "--append-system-prompt", system_prompt,
        "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(mcp_config), "--strict-mcp-config",
        "--tools", *builtin_tools,
        "--allowedTools", *allowed_tools,
        "--add-dir", str(add_dir),
        "--permission-mode", "default",
        "--no-session-persistence",
    ]


def write_mcp_config(run_dir: Path, gateway_command: list[str], env: dict[str, str] | None = None) -> Path:
    """Write the MCP config that connects the run to the gateway only. Readable by the owner alone.

    `env` carries the run's scope (role, ticket, database, worktree) into the gateway process.
    """
    if not gateway_command:
        raise ValueError("gateway command is empty")
    server = {"command": gateway_command[0], "args": gateway_command[1:]}
    if env:
        server["env"] = dict(env)
    config = {"mcpServers": {GATEWAY_SERVER: server}}
    path = run_dir / "gateway.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(config, indent=2) + "\n")
    return path


def _final_event(stdout: str) -> dict | None:
    final = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            final = event
    return final


def run(
    *,
    worktree: Path,
    prompt: str,
    system_prompt: str,
    model: str,
    run_dir: Path,
    profile: str,
    gateway_command: list[str],
    access: bool,
    gateway_env: dict[str, str] | None = None,
    timeout: float = 3600.0,
    claude_bin: str | None = None,
    runner: Runner | None = None,
) -> RunResult:
    if not access:
        raise ProviderNotAllowed(
            "Claude Code runs are not enabled on this machine; set claude_code_access in the model config"
        )
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose one of {sorted(PROFILES)}")
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise ValueError(f"{worktree} is not a git worktree")
    if not claude_bin:
        raise ProviderNotAllowed("the claude CLI is not on PATH")

    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    mcp_config = write_mcp_config(run_dir, gateway_command, gateway_env)
    spec = PROFILES[profile]
    argv = build_argv(
        claude_bin=claude_bin,
        prompt=prompt,
        system_prompt=system_prompt,
        model=model,
        mcp_config=mcp_config,
        builtin_tools=spec["builtin_tools"],
        allowed_tools=spec["allowed_tools"],
        add_dir=worktree,
    )

    output_path = run_dir / "output.jsonl"
    run_fn = runner or default_runner
    try:
        proc = run_fn(argv, worktree, timeout)
        stdout, stderr, code, timed_out = proc.stdout or "", proc.stderr or "", proc.returncode, False
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        code, timed_out = None, True

    output_path.write_text(stdout)
    (run_dir / "stderr.log").write_text(stderr[-8000:])
    return RunResult(exit_code=code, timed_out=timed_out, output_path=output_path,
                     final=_final_event(stdout))


class ClaudeCodeRunner:
    """AgentRunner over the Claude Code CLI: one `claude -p` process per run, connected to the gateway."""

    def __init__(self, *, gateway_command: list[str], claude_bin: str | None = None, runner: Runner | None = None):
        self._gateway_command = gateway_command
        self._claude_bin = claude_bin
        self._runner = runner

    def run(self, request: AgentRunRequest) -> RunResult:
        return run(
            worktree=request.worktree, prompt=request.prompt, system_prompt=request.system_prompt,
            model=request.model, run_dir=request.run_dir, profile=request.role,
            gateway_command=self._gateway_command, access=request.access, gateway_env=request.gateway_env,
            timeout=request.timeout, claude_bin=self._claude_bin, runner=self._runner)
