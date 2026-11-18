"""A git repository as a Workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def branch_name(key: str) -> str:
    """Every ticket has one branch, cut from the internal main."""
    return f"agent/{key}"


class GitWorkspace:
    def __init__(self, repo: Path, run: Run = subprocess.run):
        self._repo = Path(repo)
        self._run = run

    def branch_head(self, key: str) -> str | None:
        proc = self._run(["git", "rev-parse", "--verify", "--quiet", branch_name(key)], cwd=self._repo,
                         capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None
