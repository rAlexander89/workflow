"""`wf doctor`: checks that a machine can run the Claude Code flow.

The first flow targets Apple Silicon Macs. Each check reports ok, warn, or fail.
A failed required check makes `wf doctor` exit non-zero. The system calls are
passed in, so tests run anywhere.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from wf.model.claude_code import CLAUDE_BIN, KEY_VARS, subscription_env


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok", "warn", or "fail"
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _auth_status(claude_bin: str, env: dict[str, str]) -> dict:
    proc = subprocess.run([claude_bin, "auth", "status", "--json"], capture_output=True, text=True,
                          timeout=30, check=False, env=env)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def run_checks(
    *,
    system: str | None = None,
    machine: str | None = None,
    python_version: tuple[int, int] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    auth_status: Callable[[str, dict[str, str]], dict] = _auth_status,
    environ: dict[str, str] | None = None,
    claude_code_access: bool = False,
) -> list[Check]:
    system = system or sys.platform
    machine = machine or platform.machine()
    python_version = python_version or sys.version_info[:2]
    environ = dict(os.environ if environ is None else environ)
    checks: list[Check] = []

    is_mac_arm = system == "darwin" and machine == "arm64"
    checks.append(Check("apple silicon mac", "ok" if is_mac_arm else "fail",
                        f"{system} {machine}" if not is_mac_arm else "darwin arm64"))

    py_ok = python_version >= (3, 11)
    checks.append(Check("python 3.11+", "ok" if py_ok else "fail", f"{python_version[0]}.{python_version[1]}"))

    git = which("git")
    checks.append(Check("git", "ok" if git else "fail", git or "not on PATH"))

    claude = which(CLAUDE_BIN)
    checks.append(Check("claude cli", "ok" if claude else "fail", claude or "not on PATH; install Claude Code first"))

    if claude:
        present = [v for v in KEY_VARS if environ.get(v)]
        if present:
            checks.append(Check("api key variables", "warn",
                                f"{', '.join(present)} set here; wf runs remove them, so runs use the login"))
        else:
            checks.append(Check("api key variables", "ok", "none set"))

        status = auth_status(claude, subscription_env(environ))
        logged_in = bool(status.get("loggedIn"))
        method = status.get("authMethod", "unknown")
        if not logged_in:
            checks.append(Check("claude login", "fail", "not logged in; run `claude auth login` and choose your subscription"))
        elif method == "api_key" or status.get("apiKeySource"):
            checks.append(Check("claude login", "fail",
                                f"login uses an API key ({method}); sign in with your subscription instead"))
        else:
            checks.append(Check("claude login", "ok", f"signed in ({method})"))

    checks.append(Check("claude code access", "ok" if claude_code_access else "fail",
                        "enabled" if claude_code_access else
                        "not enabled; run `wf model set --enable-claude-code` (operator-provisioned only)"))
    return checks


def format_checks(checks: list[Check]) -> str:
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    return "\n".join(f"[{marks[c.status]}] {c.name}: {c.detail}" for c in checks)
