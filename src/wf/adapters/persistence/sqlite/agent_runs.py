"""SQLite record of agent runs, one row per run."""

from __future__ import annotations

import sqlite3

from wf.domain.execution.run_history import AgentRunRecord

SCHEMA = """
create table if not exists agent_run (
    id          integer primary key autoincrement,
    key         text not null,
    stage       text not null,
    role        text not null,
    started_at  integer not null,
    exit_code   integer,
    timed_out   integer not null default 0,
    output_path text not null
);
create index if not exists agent_run_key on agent_run (key, stage);
"""


class SqliteAgentRuns:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        conn.executescript(SCHEMA)

    def record(self, run: AgentRunRecord) -> None:
        self._conn.execute(
            "insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (run.key, str(run.stage), str(run.role), run.started_at, run.exit_code, int(run.timed_out),
             str(run.output_path)))
        self._conn.commit()

    def runs_for(self, key: str) -> list[AgentRunRecord]:
        rows = self._conn.execute(
            "select key, stage, role, started_at, exit_code, timed_out, output_path from agent_run "
            "where key = ? order by id desc", (key,)).fetchall()
        return [AgentRunRecord(r["key"], r["stage"], r["role"], r["started_at"], r["exit_code"],
                               bool(r["timed_out"]), r["output_path"]) for r in rows]
