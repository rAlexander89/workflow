"""SQLite token-usage ledger."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from wf.domain.execution.usage import AgentAudit, UsageRecord

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


class SqliteUsageLog:
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

    def largest_calls(self, limit: int = 5) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "select agent, ticket, model, input_tokens, output_tokens, created_at "
                "from llm_calls order by input_tokens + output_tokens desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
