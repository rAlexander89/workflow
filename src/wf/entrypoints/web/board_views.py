"""Web pages for the internal sprint board: the active sprint, the backlog, one ticket,
and the Jira outbox.

Reads come straight from the board's SQLite file. The only writes are the ones the user
makes here: clearing a flag and retrying an outbox item. Agents never reach these pages.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from wf.application.board.status import board_status
from wf.application.tracker.outbox import RETRYABLE
from wf.bootstrap import Container
from wf.domain.board import stages as mapping
from wf.domain.board.errors import BoardError
from wf.domain.delivery.roles import STAGE_ROLE

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


def mount_board(app: FastAPI, container: Container) -> None:
    def page(request: Request, name: str, active: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"active": active, **ctx})

    def sync_line(s, kind: str | None = None) -> dict:
        status = board_status(s.queries, time.time(), kind)
        run, age = status["last_sync"], status["age_seconds"]
        return {
            "status": status,
            "run": dict(run) if run else None,
            "age_text": None if age is None else f"{age // 3600}h {age % 3600 // 60}m ago",
            "stale": age is not None and age > 24 * 3600,
            "pending": len(s.outbox.listing("pending")),
        }

    @app.get("/board", response_class=HTMLResponse)
    def board_page(request: Request):
        with container.session() as s:
            rows = s.queries.active_board()
            return page(request, "board.html", "board",
                        groups=group_by_stage(rows), total=len(rows),
                        blocked=[dict(r) for r in s.queries.unmapped_stages()],
                        sync=sync_line(s, "active"), title="Active sprint")

    @app.get("/board/backlog", response_class=HTMLResponse)
    def backlog_page(request: Request, sent: int = 0):
        from datetime import datetime
        from wf.entrypoints.web import pipeline as _pipeline
        with container.session() as s:
            rows = []
            for r in s.queries.backlog():
                t = dict(r)
                parts = (t.get("assignee_name") or "").split()
                t["initials"] = "".join(p[0] for p in parts[:2]).upper()
                t["stage_label"] = _pipeline.STAGE_INFO.get(t["stage"], ("To do", ""))[0] if t["stage"] else "To do"
                t["updated_text"] = datetime.fromtimestamp(t["jira_updated"]).strftime("%b %d") if t.get("jira_updated") else ""
                rows.append(t)
            rows.sort(key=lambda t: t.get("jira_updated") or 0, reverse=True)
            active = s.queries.active_sprint()
            return page(request, "backlog.html", "backlog", tickets=rows, sent=bool(sent),
                        active_sprint=active, sync=sync_line(s))

    @app.get("/board/item/{key}", response_class=HTMLResponse)
    def item_page(request: Request, key: str):
        from datetime import datetime
        from wf.entrypoints.web import pipeline as _pipeline

        def when(ts):
            return datetime.fromtimestamp(ts).strftime("%b %d, %Y %H:%M") if ts else ""

        with container.session() as s:
            detail = s.queries.ticket_detail(key)
            if detail is None:
                return HTMLResponse("No such ticket.", status_code=404)
            t = detail["ticket"]
            active = s.queries.active_sprint()
            sprint = {"name": s.queries.sprint_name(t["sprint_id"])} if t["sprint_id"] else None
            links = s.queries.ticket_links(key)
            comments = [dict(x, when=when(x["created_at"])) for x in detail["comments"]]
            events = [dict(x, when=when(x["at"])) for x in reversed(detail["events"])]
            stage = t["stage"]
            return page(request, "item.html", "backlog", ticket=t, links=links, comments=comments,
                        events=events, labels=json.loads(t["labels"] or "[]"), stage=stage,
                        stage_label=_pipeline.STAGE_INFO.get(stage, ("To do", ""))[0] if stage else "Not started",
                        active_sprint=dict(active) if active else None,
                        in_active_sprint=bool(active and t["sprint_id"] == active["id"]),
                        sprint_name=sprint["name"] if sprint else None,
                        updated_text=when(t.get("jira_updated")), synced_text=when(t.get("synced_at")))

    @app.get("/board/ticket/{key}", response_class=HTMLResponse)
    def ticket_page(request: Request, key: str, notice: str = ""):
        from wf.entrypoints.web import pipeline as _pipeline
        from datetime import datetime

        def when(ts):
            return datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else ""

        settings = container.settings()
        with container.session() as s:
            detail = s.queries.ticket_detail(key)
            if detail is None:
                return HTMLResponse("No such ticket.", status_code=404)
            items = s.outbox.for_ticket(key)
            runs = [dataclasses.asdict(r) for r in s.agent_runs.runs_for(key)]
            for r in runs:
                r["when"] = when(r["started_at"])
            events = [dict(e) for e in reversed(detail["events"])]
            for e in events:
                e["when"] = when(e["at"])
            noise = [e for e in events if e["actor"] == "sync" or (e["actor"] == "outbox" and e["kind"] == "stage")]
            events = [e for e in events if e not in noise]
            for c_ in detail["comments"]:
                c_["preview"] = (c_["body"] or "").strip().splitlines()[0][:160] if c_.get("body") else ""
            observer = s.observer(settings)
            failure_list = observer.failures(key)
            for r in runs:
                if r["exit_code"] not in (0, None) or r["timed_out"]:
                    failure_list.append({"step": f"{r['role']} agent on {r['stage']}", "job": "agent run",
                                         "url": "", "reasons": [f"exit {r['exit_code']}" if not r["timed_out"]
                                                                else "timed out"],
                                         "output": r["output_path"], "stderr": r["output_path"].replace("output.jsonl", "stderr.log")})
            t = detail["ticket"]
            t["flag_list"] = json.loads(t["flags"] or "[]")
            t["label_list"] = json.loads(t["labels"] or "[]")
            next_role = None if t["flag_list"] else STAGE_ROLE.get(t["stage"] or "")
            pr_url = s.tickets.last_note(key, "pr")

            def prod_ready() -> bool:
                if not t["stage"] or t["stage"] not in ("regression", "performance"):
                    return False
                try:
                    return s.deploy(settings).prod_ready(key)
                except Exception:
                    return False
            gh = s.pull_requests.status(pr_url) if pr_url else {}
            flag_reason = next((e["detail"] for e in events if e["kind"] == "flag"), "") if t["flag_list"] else ""
            return page(request, "ticket.html", "agents", ticket=t, comments=detail["comments"],
                        events=events, noise_count=len(noise), flag_reason=flag_reason, outbox_items=items, runs=runs, next_role=next_role,
                        notice=notice,
                        stages=_pipeline.progress(t["stage"]), title=key, failures=failure_list,
                        marker=_pipeline.stage_marker(t["stage"]),
                        flow=_pipeline.flow_states(
                            t["stage"],
                            last_run_failed=bool(runs and (runs[0]["exit_code"] != 0 or runs[0]["timed_out"])),
                            perf_requested=s.tickets.perf_requested(key),
                            pr_url=pr_url, pr_merged=gh.get("merged", False),
                            dev_deployed=observer.dev_deployed(key), prod_deployed=observer.prod_deployed(key),
                            dev_failed=bool((observer.latest(key, "dev:") or "").startswith("dev:failed")),
                            dev_running=observer.dev_running(key),
                            pr_checks=gh.get("checks", "none"), pr_closed=gh.get("ok") and gh.get("state") == "CLOSED",
                            flagged=bool(t["flag_list"]),
                            prod_ready=prod_ready()))

    @app.get("/board/ticket/{key}/deploy-panel", response_class=HTMLResponse)
    def deploy_panel(request: Request, key: str):
        settings = container.settings()
        with container.session() as s:
            deploy = s.deploy_graph(settings).dev_deploy(key)
        return templates.TemplateResponse(request, "_deploy.html", {"deploy": deploy})

    @app.post("/board/ticket/{key}/send-back")
    def send_back(request: Request, key: str, to: str = Form(...), reason: str = Form(...)):
        """Send a ticket back to an earlier stage with a reason. The pipeline restarts from there."""
        allowed = {"in_dev", "pr_open", "ready_for_qa"}
        text = reason.strip()
        try:
            with container.session() as s:
                if to in allowed and text:
                    s.tickets.add_comment(key, "user", f"Sent back to {to}: {text}", origin="user")
                    s.tickets.move_stage(key, to, "user", f"sent back: {text[:120]}")
        except BoardError:
            pass
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

    @app.post("/board/ticket/{key}/answer")
    def answer_flag(request: Request, key: str, answer: str = Form("")):
        """The human's answer to a flagged question: it becomes a comment the next run reads,
        and the flags clear, so the pipeline can continue."""
        text = answer.strip()
        if not text:
            return RedirectResponse(f"/board/ticket/{key}", status_code=303)
        with container.session() as s:
            s.tickets.add_comment(key, "user", text, origin="user")
            for flag in list(s.tickets.get(key).flags):
                s.tickets.clear_flag(key, flag, "user")
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

    @app.post("/board/ticket/{key}/flag/clear")
    def clear_flag(key: str, flag: str = Form(...)):
        try:
            with container.session() as s:
                s.tickets.clear_flag(key, flag, "user")
        except BoardError:
            pass
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

    @app.get("/board/outbox", response_class=HTMLResponse)
    def outbox_page(request: Request):
        with container.session() as s:
            return page(request, "outbox.html", "outbox", items=s.outbox.listing(),
                        retryable=RETRYABLE, title="Outbox")

    @app.post("/board/outbox/{item_id}/retry")
    def retry_item(item_id: int):
        try:
            with container.session() as s:
                s.outbox.retry(item_id)
        except BoardError:
            pass
        return RedirectResponse("/board/outbox", status_code=303)
