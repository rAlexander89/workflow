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
        from wf import agents as _agents
        from wf.web import pipeline as _pipeline
        from datetime import datetime

        def when(ts):
            return datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else ""

        c = conn()
        try:
            detail = board.ticket_detail(c, key)
            if detail is None:
                return HTMLResponse("No such ticket.", status_code=404)
            _agents.ensure_schema(c)
            items = [dict(r) for r in c.execute(
                "select * from outbox where ticket_key = ? and kind = 'transition' order by id", (key,)).fetchall()]
            runs = [dict(r) for r in c.execute(
                "select * from agent_run where key = ? order by id desc", (key,)).fetchall()]
            for r in runs:
                r["when"] = when(r["started_at"])
            events = [dict(e) for e in reversed(detail["events"])]
            for e in events:
                e["when"] = when(e["at"])
            noise = [e for e in events if e["actor"] == "sync" or (e["actor"] == "outbox" and e["kind"] == "stage")]
            events = [e for e in events if e not in noise]
            for c_ in detail["comments"]:
                c_["preview"] = (c_["body"] or "").strip().splitlines()[0][:160] if c_.get("body") else ""
            t = detail["ticket"]
            t["flag_list"] = json.loads(t["flags"] or "[]")
            t["label_list"] = json.loads(t["labels"] or "[]")
            next_role = None if t["flag_list"] else _agents.STAGE_ROLE.get(t["stage"] or "")
            pr_url = (c.execute("select detail from ticket_event where ticket_key=? and kind='pr' "
                                "order by id desc limit 1", (key,)).fetchone() or [None])[0]
            from wf import github_status as _gh
            from wf import observe as _observe

            def agents_prod_ready(conn, k):
                try:
                    from wf import config as _cfg
                    cfg = _cfg.load(_cfg.default_path())
                    repo = Path(next(r.path for r in cfg.repos))
                    return _agents.prod_ready(conn, k, repo)
                except Exception:
                    return False
            gh = _gh.pr_status(pr_url) if pr_url else {}
            flag_reason = next((e["detail"] for e in events if e["kind"] == "flag"), "") if t["flag_list"] else ""
            return page(request, "ticket.html", "agents", ticket=t, comments=detail["comments"],
                        events=events, noise_count=len(noise), flag_reason=flag_reason, outbox_items=items, runs=runs, next_role=next_role,
                        stages=_pipeline.progress(t["stage"]), title=key,
                        flow=_pipeline.flow_states(
                            t["stage"],
                            last_run_failed=bool(runs and (runs[0]["exit_code"] != 0 or runs[0]["timed_out"])),
                            perf_requested=board.perf_requested(c, key),
                            pr_url=pr_url, pr_merged=gh.get("merged", False),
                            dev_deployed=_observe.dev_deployed(c, key), prod_deployed=_observe.prod_deployed(c, key),
                            pr_checks=gh.get("checks", "none"), pr_closed=gh.get("ok") and gh.get("state") == "CLOSED",
                            flagged=bool(t["flag_list"]),
                            prod_ready=bool(t["stage"] in ("regression", "performance") and
                                            agents_prod_ready(c, key)) if t["stage"] else False))
        finally:
            c.close()

    @app.post("/board/ticket/{key}/send-back")
    def send_back(request: Request, key: str, to: str = Form(...), reason: str = Form(...)):
        """Send a ticket back to an earlier stage with a reason. The pipeline restarts from there."""
        allowed = {"in_dev", "pr_open", "ready_for_qa"}
        text = reason.strip()
        c = conn()
        try:
            if to in allowed and text:
                board.add_comment(c, key, "user", f"Sent back to {to}: {text}", origin="user")
                board.move_stage(c, key, to, "user", f"sent back: {text[:120]}")
        except board.BoardError:
            pass
        finally:
            c.close()
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

    @app.post("/board/ticket/{key}/answer")
    def answer_flag(request: Request, key: str, answer: str = Form("")):
        """The human's answer to a flagged question: it becomes a comment the next run reads,
        and the flags clear, so the pipeline can continue."""
        text = answer.strip()
        c = conn()
        try:
            if not text:
                return RedirectResponse(f"/board/ticket/{key}", status_code=303)
            board.add_comment(c, key, "user", text, origin="user")
            flags = json.loads(c.execute("select flags from ticket where key = ?", (key,)).fetchone()["flags"])
            for flag in flags:
                board.clear_flag(c, key, flag, "user")
        finally:
            c.close()
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

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
