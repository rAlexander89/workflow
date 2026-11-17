"""SQLite record of agent runs, one row per run."""

from __future__ import annotations

import sqlite3

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

    def record(self, key, stage, role, started_at, exit_code, timed_out, output_path) -> None:
        self._conn.execute(
            "insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (key, stage, role, started_at, exit_code, int(timed_out), str(output_path)))
        self._conn.commit()

    def last_role_at(self, key: str, stage: str) -> str | None:
        row = self._conn.execute(
            "select role from agent_run where key = ? and stage = ? order by id desc limit 1", (key, stage)).fetchone()
        return None if row is None else row["role"]

    def last_run(self, key: str, stage: str) -> dict | None:
        row = self._conn.execute(
            "select role, exit_code, timed_out, started_at from agent_run where key = ? and stage = ? "
            "order by id desc limit 1", (key, stage)).fetchone()
        return None if row is None else dict(row)

    def failed_in_a_row(self, key: str, stage: str) -> int:
        rows = self._conn.execute(
            "select exit_code, timed_out from agent_run where key = ? and stage = ? order by id desc",
            (key, stage)).fetchall()
        streak = 0
        for r in rows:
            if r["timed_out"] or r["exit_code"] != 0:
                streak += 1
            else:
                break
        return streak

    def product_rounds(self, key: str) -> int:
        return self._conn.execute(
            "select count(*) from agent_run where key = ? and role = 'product'", (key,)).fetchone()[0]

    def runs_for(self, key: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "select * from agent_run where key = ? order by id desc", (key,)).fetchall()]

    def last_exit_code(self, key: str, role: str | None = None) -> int | None:
        if role is None:
            row = self._conn.execute(
                "select exit_code from agent_run where key = ? order by id desc limit 1", (key,)).fetchone()
        else:
            row = self._conn.execute(
                "select exit_code from agent_run where key = ? and role = ? order by id desc limit 1",
                (key, role)).fetchone()
        return None if row is None else row["exit_code"]
