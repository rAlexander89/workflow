"""Model usage: what one call used, and the per-agent audit built from many calls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageRecord:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    run_id: str | None = None
    ticket: str | None = None
    duration_ms: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class AgentAudit:
    agent: str
    calls: int
    avg_input: float
    avg_output: float
    total_input: int
    total_output: int
    cache_hit_ratio: float  # cache reads as a share of all input-side tokens
