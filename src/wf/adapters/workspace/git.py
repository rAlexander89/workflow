"""A git repository as a Workspace: one branch and worktree per ticket."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from wf.domain.delivery.branches import branch_for

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def run_git(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


class GitWorkspace:
    def __init__(self, repo: Path, runs_dir: Path | None = None, runner: Runner = run_git):
        self._repo = Path(repo)
        self._worktrees = None if runs_dir is None else Path(runs_dir) / "worktrees"
        self._run = runner

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        proc = self._run(["git", *args], cwd or self._repo)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _worktree(self, key: str) -> Path:
        if self._worktrees is None:
            raise RuntimeError("this workspace has no runs directory, so it has no worktrees")
        return self._worktrees / key

    def branch_head(self, key: str) -> str | None:
        proc = self._run(["git", "rev-parse", "--verify", "--quiet", branch_for(key)], self._repo)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def worktree_exists(self, key: str) -> bool:
        return self._worktree(key).exists()

    def worktree_head(self, key: str) -> str:
        return self._git("rev-parse", "HEAD", cwd=self._worktree(key))

    def merge_branch(self, key: str) -> None:
        branch = branch_for(key)
        self._git("checkout", "main")
        self._git("merge", "--no-ff", branch, "-m", f"Merge {branch} ({key})")

    def main_head(self) -> str:
        return self._git("rev-parse", "main")

    def remote_url(self) -> str | None:
        """The repo's origin URL, or None if it has none. Always asks git directly."""
        proc = run_git(["git", "remote", "get-url", "origin"], self._repo)
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None

    def push_branch(self, key: str) -> tuple[bool, str]:
        """Push the ticket's branch to origin. (ok, the end of git's error when not ok)."""
        proc = self._run(["git", "push", "-u", "origin", branch_for(key)], self._repo)
        return proc.returncode == 0, proc.stderr.strip()[-300:]

    def stray_commits(self, key: str) -> tuple[bool, list[str]]:
        fetched = self._run(["git", "fetch", "-q", "origin", "main"], self._repo)
        if fetched.returncode != 0 and self._run(
                ["git", "rev-parse", "--verify", "--quiet", "origin/main"], self._repo).returncode != 0:
            return False, ["could not compare with GitHub's main: no origin/main to compare against"]
        log = self._run(["git", "log", "--format=%s", f"origin/main..{branch_for(key)}"], self._repo)
        if log.returncode != 0:
            return False, ["could not read the branch's commits"]
        stray = [line for line in log.stdout.splitlines() if line.strip() and key not in line]
        return (not stray), stray


@dataclass
class TestOutcome:
    ok: bool
    output: str = ""


class ShellTestRunner:
    """Runs the test command in the ticket's worktree."""

    __test__ = False  # not a pytest class

    def __init__(self, runs_dir: Path):
        self._worktrees = Path(runs_dir) / "worktrees"

    def run(self, key: str, command: str) -> TestOutcome:
        proc = subprocess.run(command, shell=True, cwd=self._worktrees / key, capture_output=True, text=True,
                              check=False)
        return TestOutcome(proc.returncode == 0, proc.stdout + proc.stderr)
