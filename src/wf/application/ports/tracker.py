"""Tracker ports for reading issues and checking links, used by the web pages and the CLI."""

from __future__ import annotations

from typing import Protocol


class TrackerError(RuntimeError):
    """The tracker could not be reached or refused the request."""


class IssueReader(Protocol):
    def get_issue_changelog(self, key: str) -> dict:
        """The issue with its changelog, from the tracker's REST API."""
        ...

    def search_keys(self, jql: str, page_size: int = 100) -> list[str]:
        """The keys of the issues a JQL search finds."""
        ...

    def check_board(self, board) -> "CheckResult": ...


class CheckResult(Protocol):
    ok: bool
    detail: str
