"""Delivery ports: the places the pipeline reads state from that are not the board."""

from __future__ import annotations

from typing import Protocol


class Workspace(Protocol):
    """The repository the agents work in, with one branch per ticket."""

    def branch_head(self, key: str) -> str | None:
        """The commit the ticket's branch points at, or None if the branch does not exist."""
        ...


class DeployObserver(Protocol):
    """What has reached the deployed environments."""

    def dev_deployed(self, key: str) -> bool:
        """True once the ticket's merge has been deployed to dev."""
        ...
