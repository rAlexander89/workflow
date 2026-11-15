"""The workflow graph shell.

Every stage is a placeholder that records that it ran. What is real here is the
harness: state is checkpointed to SQLite after each step, a run can stop at the
human gate and resume later on the same thread id, and the gate sits before any
development work.

Stages: product -> staff -> gate (human) -> dev -> qa
"""

from __future__ import annotations

import operator
import sqlite3
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class WorkflowState(TypedDict):
    request: str
    steps: Annotated[list[str], operator.add]
    approved: bool


def product(state: WorkflowState) -> dict[str, Any]:
    return {"steps": ["product"]}


def staff(state: WorkflowState) -> dict[str, Any]:
    return {"steps": ["staff"]}


def gate(state: WorkflowState) -> dict[str, Any]:
    # Pauses the run. It resumes with Command(resume=True) or Command(resume=False).
    decision = interrupt({"question": "Approve development work?", "request": state["request"]})
    return {"steps": ["gate"], "approved": bool(decision)}


def dev(state: WorkflowState) -> dict[str, Any]:
    return {"steps": ["dev"]}


def qa(state: WorkflowState) -> dict[str, Any]:
    return {"steps": ["qa"]}


def _after_gate(state: WorkflowState) -> str:
    return "dev" if state["approved"] else END


def build(checkpointer) -> Any:
    g = StateGraph(WorkflowState)
    for name, fn in [("product", product), ("staff", staff), ("gate", gate), ("dev", dev), ("qa", qa)]:
        g.add_node(name, fn)
    g.add_edge(START, "product")
    g.add_edge("product", "staff")
    g.add_edge("staff", "gate")
    g.add_conditional_edges("gate", _after_gate, {"dev": "dev", END: END})
    g.add_edge("dev", "qa")
    g.add_edge("qa", END)
    return g.compile(checkpointer=checkpointer)


def open_checkpointer(db_path: Path) -> tuple[SqliteSaver, sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn), conn


def start(db_path: Path, thread_id: str, request: str) -> dict[str, Any]:
    saver, conn = open_checkpointer(db_path)
    try:
        app = build(saver)
        cfg = {"configurable": {"thread_id": thread_id}}
        out = app.invoke({"request": request, "steps": [], "approved": False}, cfg)
        return _summarise(app, cfg, out)
    finally:
        conn.close()


def resume(db_path: Path, thread_id: str, approved: bool) -> dict[str, Any]:
    saver, conn = open_checkpointer(db_path)
    try:
        app = build(saver)
        cfg = {"configurable": {"thread_id": thread_id}}
        out = app.invoke(Command(resume=approved), cfg)
        return _summarise(app, cfg, out)
    finally:
        conn.close()


def _summarise(app, cfg, out) -> dict[str, Any]:
    snapshot = app.get_state(cfg)
    waiting = bool(snapshot.next) and "gate" in snapshot.next
    return {
        "steps": out.get("steps", []),
        "status": "waiting_for_approval" if waiting else "done",
        "next": list(snapshot.next),
    }


def snapshot(db_path: Path, thread_id: str) -> dict[str, Any]:
    """Current steps and status of a run, read from its checkpoint without resuming it."""
    saver, conn = open_checkpointer(db_path)
    try:
        app = build(saver)
        cfg = {"configurable": {"thread_id": thread_id}}
        state = app.get_state(cfg)
        values = state.values or {}
        return {
            "steps": values.get("steps", []),
            "status": "waiting_for_approval" if "gate" in state.next else ("done" if not state.next else "running"),
            "next": list(state.next),
        }
    finally:
        conn.close()
