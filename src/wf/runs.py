"""Run registry: one row per workflow run, for the run list and status pages.

The LangGraph checkpoints live in the same SQLite file under their own tables.
This table holds what the web pages need without replaying the graph.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
create table if not exists runs (
    id text primary key,
    request text not null,
    status text not null,
    created_at integer not null
)
"""


@dataclass(frozen=True)
class Run:
    id: str
    request: str
    status: str
    created_at: int


class RunStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        with closing(self._connect()) as conn, conn:
            conn.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, run_id: str, request: str, status: str = "running") -> Run:
        run = Run(run_id, request, status, int(time.time()))
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "insert into runs (id, request, status, created_at) values (?, ?, ?, ?)",
                (run.id, run.request, run.status, run.created_at),
            )
        return run

    def set_status(self, run_id: str, status: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("update runs set status = ? where id = ?", (status, run_id))

    def get(self, run_id: str) -> Run | None:
        with closing(self._connect()) as conn:
            row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        return Run(**dict(row)) if row else None

    def list(self) -> list[Run]:
        with closing(self._connect()) as conn:
            rows = conn.execute("select * from runs order by created_at desc").fetchall()
        return [Run(**dict(r)) for r in rows]
