"""Local token-usage ledger and efficiency audit.

Every model call an agent makes is recorded here, in the same local SQLite file as
the rest of the workflow record. Nothing leaves the machine. The audit reads the
ledger and reports, per agent: call counts, average input size, cache hit ratio,
and the largest calls, so prompt bloat and missed caching show up.

Cost is left null on purpose. Model prices change, so they belong in config once
we pick the models, not hard-coded here.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SCHEMA = """
create table if not exists llm_calls (
    id integer primary key autoincrement,
    created_at integer not null,
    agent text not null,
    run_id text,
    ticket text,
    model text not null,
    input_tokens integer not null,
    output_tokens integer not null,
    cache_read_tokens integer not null default 0,
    cache_write_tokens integer not null default 0,
    duration_ms integer,
    note text
);
create index if not exists llm_calls_agent on llm_calls (agent);
"""


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


class UsageLedger:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, rec: UsageRecord) -> None:
        if min(rec.input_tokens, rec.output_tokens, rec.cache_read_tokens, rec.cache_write_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "insert into llm_calls (created_at, agent, run_id, ticket, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, duration_ms, note) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(self._clock()),
                    rec.agent,
                    rec.run_id,
                    rec.ticket,
                    rec.model,
                    rec.input_tokens,
                    rec.output_tokens,
                    rec.cache_read_tokens,
                    rec.cache_write_tokens,
                    rec.duration_ms,
                    rec.note,
                ),
            )

    def audit(self) -> list[AgentAudit]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "select agent, count(*) as calls, "
                "sum(input_tokens) as tin, sum(output_tokens) as tout, "
                "sum(cache_read_tokens) as tcr, sum(cache_read_tokens + cache_write_tokens + input_tokens) as tall_in "
                "from llm_calls group by agent order by agent"
            ).fetchall()
        out = []
        for r in rows:
            calls = r["calls"]
            out.append(
                AgentAudit(
                    agent=r["agent"],
                    calls=calls,
                    avg_input=r["tin"] / calls,
                    avg_output=r["tout"] / calls,
                    total_input=r["tin"],
                    total_output=r["tout"],
                    cache_hit_ratio=(r["tcr"] / r["tall_in"]) if r["tall_in"] else 0.0,
                )
            )
        return out

    def largest_calls(self, limit: int = 5) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "select agent, ticket, model, input_tokens, output_tokens, created_at "
                "from llm_calls order by input_tokens + output_tokens desc limit ?",
                (limit,),
            ).fetchall()


def format_audit(rows: list[AgentAudit], largest: list[sqlite3.Row]) -> str:
    if not rows:
        return "no model calls recorded yet"
    lines = [
        f"{'agent':<16}{'calls':>7}{'avg in':>10}{'avg out':>10}{'total in':>12}{'total out':>12}{'cache hit':>11}",
    ]
    for a in rows:
        lines.append(
            f"{a.agent:<16}{a.calls:>7}{a.avg_input:>10.0f}{a.avg_output:>10.0f}"
            f"{a.total_input:>12}{a.total_output:>12}{a.cache_hit_ratio:>10.0%}"
        )
    if largest:
        lines.append("")
        lines.append("largest calls (input + output):")
        for r in largest:
            lines.append(
                f"  {r['agent']:<14} {r['ticket'] or '-':<12} {r['model']:<22} "
                f"in {r['input_tokens']:>7}  out {r['output_tokens']:>6}"
            )
    return "\n".join(lines)
