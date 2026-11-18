"""Delivery ports: the places the pipeline reads state from that are not the board."""

from __future__ import annotations

from typing import Protocol


class Workspace(Protocol):
    """The repository the agents work in, with one branch per ticket."""

    def branch_head(self, key: str) -> str | None:
        """The commit the ticket's branch points at, or None if the branch does not exist."""
        ...

    def worktree_exists(self, key: str) -> bool: ...

    def worktree_head(self, key: str) -> str:
        """The commit checked out in the ticket's worktree."""
        ...

    def merge_branch(self, key: str) -> None:
        """Merge the ticket's branch into the internal main, with a merge commit."""
        ...

    def main_head(self) -> str: ...

    def stray_commits(self, key: str) -> tuple[bool, list[str]]:
        """(ok, stray): ok when every commit the branch adds to the remote main carries the ticket key."""
        ...


class DeployObserver(Protocol):
    """What has reached the deployed environments."""

    def dev_deployed(self, key: str) -> bool:
        """True once the ticket's merge has been deployed to dev."""
        ...


class TestResult(Protocol):
    ok: bool
    output: str


class TestRunner(Protocol):
    """Runs the project's tests in a ticket's worktree."""

    def run(self, key: str, command: str) -> TestResult: ...


class PullRequests(Protocol):
    """The hosted pull requests."""

    def status(self, url: str) -> dict:
        """ok, merged, state, checks, commit_subjects, head (and error when not ok)."""
        ...
