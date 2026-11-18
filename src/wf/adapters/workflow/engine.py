"""The workflow engine port over LangGraph, checkpointed in the board's SQLite file."""

from __future__ import annotations

from pathlib import Path

from wf.adapters.workflow import langgraph as graph


class LangGraphEngine:
    def __init__(self, db_path: Path):
        self._db = Path(db_path)

    def start(self, run_id: str, request: str) -> dict:
        return graph.start(self._db, run_id, request)

    def resume(self, run_id: str, approved: bool) -> dict:
        return graph.resume(self._db, run_id, approved=approved)

    def snapshot(self, run_id: str) -> dict:
        return graph.snapshot(self._db, run_id)
