"""Execution ports: where an agent's prompt comes from, and what runs it."""

from __future__ import annotations

from typing import Protocol

from wf.domain.execution.agent_run import AgentRunRequest, RunResult


class PromptLibrary(Protocol):
    def role_prompt(self, role: str) -> str:
        """The role's system prompt. Raises FileNotFoundError if the role has none."""
        ...


class AgentRunner(Protocol):
    """Runs one agent in a ticket's worktree, connected to the gateway with the request's scope."""

    def run(self, request: AgentRunRequest) -> RunResult: ...


class Toolbox(Protocol):
    """What an agent run may do to its own worktree."""

    def head(self) -> str:
        """The commit checked out in the run's worktree. Raises ValueError if the run has no worktree."""
        ...

    def stray_commits(self) -> tuple[bool, list[str]]:
        """(ok, stray): ok when every commit the branch adds to the remote main carries the ticket key."""
        ...

    def run_tests(self) -> dict:
        """exit_code and the tail of the output."""
        ...

    def commit(self, message: str) -> str:
        """Commit everything in the worktree to the run's branch. Returns the head commit."""
        ...


class AppSession(Protocol):
    """The project's app, as one agent run uses it: start it, fetch pages, sign in as its bot."""

    def start_app(self) -> dict: ...

    def fetch_local(self, path: str) -> dict: ...

    def stop_app(self) -> dict: ...

    def bot_login(self) -> dict: ...

    def bot_logout(self) -> dict: ...
