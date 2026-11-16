"""Web pages for the internal sprint board: the active sprint, the backlog, one ticket,
and the Jira outbox.

Reads come straight from the board's SQLite file. The only writes are the ones the user
makes here: clearing a flag and retrying an outbox item. Agents never reach these pages.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from wf import board, mapping, outbox

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

UNMAPPED = "(unmapped)"


def group_by_stage(rows) -> list[tuple[str, list[dict]]]:
    """Rows grouped in workflow order. Tickets with no stage come last."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["stage"] or UNMAPPED, []).append(dict(r))
    ordered = [(s, groups[s]) for s in mapping.STAGES if s in groups]
    if UNMAPPED in groups:
        ordered.append((UNMAPPED, groups[UNMAPPED]))
    return ordered


def _flags(row) -> list[str]:
    return json.loads(row["flags"]) if row["flags"] else []


def mount_board(app: FastAPI, db_path: Path) -> None:
    db_path = Path(db_path)

    def conn():
        return board.connect(db_path)

    def page(request: Request, name: str, active: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"active": active, **ctx})

    def sync_line(c, kind: str | None = None) -> dict:
        s = board.status(c, time.time())
        if kind is None:
            run = s["last_sync"]
        else:
            row = c.execute("select * from sync_run where kind = ? order by id desc limit 1", (kind,)).fetchone()
            run = row
        age = None if run is None or run["finished_at"] is None else int(time.time()) - run["finished_at"]
        return {
            "status": s,
            "run": dict(run) if run else None,
            "age_text": None if age is None else f"{age // 3600}h {age % 3600 // 60}m ago",
            "stale": age is not None and age > 24 * 3600,
            "pending": len(outbox.listing(c, "pending")),
        }

    @app.get("/board", response_class=HTMLResponse)
    def board_page(request: Request):
        c = conn()
        try:
            rows = board.active_board(c)
            return page(request, "board.html", "board",
                        groups=group_by_stage(rows), total=len(rows),
                        blocked=[dict(r) for r in board.unmapped_stages(c)],
                        sync=sync_line(c, "active"), title="Active sprint")
        finally:
            c.close()

    @app.get("/board/backlog", response_class=HTMLResponse)
    def backlog_page(request: Request):
        c = conn()
        try:
            rows = board.backlog(c)
            return page(request, "backlog.html", "backlog",
                        groups=group_by_stage(rows), total=len(rows), sync=sync_line(c))
        finally:
            c.close()

    @app.get("/board/ticket/{key}", response_class=HTMLResponse)
    def ticket_page(request: Request, key: str):
        c = conn()
        try:
            detail = board.ticket_detail(c, key)
            if detail is None:
                return HTMLResponse("No such ticket.", status_code=404)
            items = [dict(r) for r in c.execute(
                "select * from outbox where ticket_key = ? order by id", (key,)).fetchall()]
            t = detail["ticket"]
            t["flag_list"] = json.loads(t["flags"] or "[]")
            t["label_list"] = json.loads(t["labels"] or "[]")
            return page(request, "ticket.html", "board", ticket=t, comments=detail["comments"],
                        events=detail["events"], outbox_items=items, title=key)
        finally:
            c.close()

    @app.post("/board/ticket/{key}/flag/clear")
    def clear_flag(key: str, flag: str = Form(...)):
        c = conn()
        try:
            board.clear_flag(c, key, flag, actor="user")
        except board.BoardError:
            pass
        finally:
            c.close()
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

    @app.get("/board/outbox", response_class=HTMLResponse)
    def outbox_page(request: Request):
        c = conn()
        try:
            items = outbox.listing(c)
            return page(request, "outbox.html", "outbox", items=items,
                        retryable=outbox.RETRYABLE, title="Outbox")
        finally:
            c.close()

    @app.post("/board/outbox/{item_id}/retry")
    def retry_item(item_id: int):
        c = conn()
        try:
            outbox.retry(c, item_id)
        except board.BoardError:
            pass
        finally:
            c.close()
        return RedirectResponse("/board/outbox", status_code=303)
