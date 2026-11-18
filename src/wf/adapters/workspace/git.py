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
    def __init__(self, repo: Path, runs_dir: Path | None = None, runner: Runner = run_git,
                 worktrees: Path | None = None):
        self._repo = Path(repo)
        self._worktrees = Path(worktrees) if worktrees is not None else (
            None if runs_dir is None else Path(runs_dir) / "worktrees")
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

    def ensure_worktree(self, key: str) -> Path:
        branch = branch_for(key)
        worktrees = self._worktrees.resolve() if self._worktrees is not None else self._worktree(key)
        path = worktrees / key
        if path.exists():
            return path
        worktrees.mkdir(parents=True, exist_ok=True)
        if self._git("branch", "--list", branch):
            self._git("worktree", "add", str(path), branch)
        else:
            has_origin = run_git(["git", "rev-parse", "--verify", "--quiet", "origin/main"], self._repo).returncode == 0
            base = "origin/main" if has_origin else "main"
            self._git("worktree", "add", "-b", branch, str(path), base)
        return path

    def sync_with_main(self, key: str) -> None:
        """Fetch GitHub's main and rebase the ticket's branch onto it. The rebase is undone if it conflicts."""
        if run_git(["git", "remote", "get-url", "origin"], self._repo).returncode != 0:
            return  # a repo with no hosted remote: its local main is the main
        worktree = self._worktree(key)
        fetch = run_git(["git", "fetch", "-q", "origin", "main"], self._repo)
        if fetch.returncode != 0:
            raise RuntimeError(f"could not fetch the latest main: {fetch.stderr.strip()[-200:]}")
        rebase = run_git(["git", "rebase", "origin/main"], worktree)
        if rebase.returncode != 0:
            run_git(["git", "rebase", "--abort"], worktree)
            raise RuntimeError("could not rebase onto the latest main; a person needs to resolve the conflict")

    def diff_range(self, key: str) -> str:
        proc = run_git(["git", "log", "main", "--merges", "--format=%H", "-1",
                        f"--grep=^Merge {branch_for(key)} "], self._repo)
        merge_commit = proc.stdout.strip()
        if merge_commit:
            return f"{merge_commit}^1..{merge_commit}"
        return f"main...{branch_for(key)}"

    def diff_stat(self, key: str) -> list[str]:
        return run_git(["git", "diff", "--stat", self.diff_range(key)], self._repo).stdout.strip().splitlines()

    def diff_text(self, key: str) -> str:
        return run_git(["git", "diff", self.diff_range(key)], self._repo).stdout

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
