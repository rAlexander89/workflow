"""Adapter for the Claude Code CLI (`claude -p`), run on this host under the operator's own Claude Code login.

Only for machines whose operator has provisioned Claude Code for this workflow. The
adapter refuses to start unless `claude_code_access` is true in the model config. It
never reads an API key, and built-in tools are disabled for plain completions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Callable

from wf.model import Message, ModelError, ModelRequest, ModelResponse, ProviderNotAllowed

CLAUDE_BIN = "claude"
SHORT = 300

# If either of these is set, Claude Code bills that key instead of the subscription login.
# Runs must not inherit them, or a subscription run quietly becomes API spend.
KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# (argv, timeout seconds) -> CompletedProcess. Injected so tests run offline.
Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def subscription_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with the API key variables removed."""
    env = dict(os.environ if environ is None else environ)
    for name in KEY_VARS:
        env.pop(name, None)
    return env


def default_runner(argv: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                          env=subscription_env())


def _render_messages(messages: tuple[Message, ...]) -> str:
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content
    return "\n\n".join(f"{m.role}: {m.content}" for m in messages)


class ClaudeCodeModel:
    name = "claude-code"

    def __init__(self, cfg, env: dict[str, str], transport=None, runner: Runner | None = None,
                 which: Callable[[str], str | None] | None = None):
        if not cfg.claude_code_access:
            raise ProviderNotAllowed(
                "claude-code is not enabled on this machine; set claude_code_access in the model config"
            )
        self.model = cfg.model
        self._run = runner or default_runner
        binary = (which or shutil.which)(CLAUDE_BIN)
        if not binary:
            raise ModelError("the claude CLI is not on PATH")
        self._bin = binary

    def complete(self, request: ModelRequest) -> ModelResponse:
        argv = [
            self._bin, "-p", _render_messages(request.messages),
            "--model", self.model,
            "--output-format", "json",
            "--tools", "",  # no built-in tools for a plain completion
            "--no-session-persistence",
        ]
        if request.system:
            argv += ["--system-prompt", request.system]

        started = time.monotonic()
        try:
            proc = self._run(argv, 600.0)
        except subprocess.TimeoutExpired:
            raise ModelError("claude timed out") from None
        elapsed = int((time.monotonic() - started) * 1000)

        try:
            data = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            raise ModelError(f"claude returned non-JSON output (exit {proc.returncode})") from None
        if proc.returncode != 0 or data.get("is_error"):
            detail = str(data.get("result") or proc.stderr or "")[:SHORT]
            raise ModelError(f"claude failed (exit {proc.returncode}): {detail}")

        usage = data.get("usage") or {}
        return ModelResponse(
            text=str(data.get("result", "")),
            model=self.model,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            duration_ms=elapsed,
        )
