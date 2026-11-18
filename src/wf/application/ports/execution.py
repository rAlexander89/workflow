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
