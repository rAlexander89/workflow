"""A workflow run, as the run list and status pages show it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Run:
    id: str
    request: str
    status: str
    created_at: int
