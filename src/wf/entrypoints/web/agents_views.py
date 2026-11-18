"""Agents page: start the agents, merge, and publish from the browser.

Each action runs in a background thread, because an agent run takes minutes. The page
shows what is running, the last result, and each ticket's next step. The handlers only translate HTTP into
calls on the use cases the composition root hands them, so the UI and the CLI can't disagree.
"""

from __future__ import annotations

import html as _html
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from wf.bootstrap import Container
from wf.domain.board.errors import BoardError
from wf.domain.delivery import github_webhook
from wf.domain.delivery.branches import branch_for
from wf.domain.delivery.roles import STAGE_ROLE
from wf.domain.tracker import jira_webhook
from wf.entrypoints.web import activity
from wf.entrypoints.web import pipeline as stage_view
from wf.application.harness.run_ticket import NotStarted

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

_wake = threading.Event()  # set by a relevant GitHub webhook: the auto loop checks now, not in 15s
_jira_changed = threading.Event()  # set by a Jira webhook: the auto loop reads Jira again now
_log = activity.log
_start = activity.start


def mount_agents(app: FastAPI, container: Container) -> None:

    def page(request: Request, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, "agents.html", {"active": "agents", **ctx})

    def context() -> dict:
        settings = container.settings()
        with container.session() as s:
            pipeline = s.pipeline(settings)
            deploy = s.deploy(settings)
            rows = sorted(s.queries.pipeline_rows(), key=lambda r: r["key"])
            queued = s.outbox.pending_count()
            paused = s.pause.is_paused()
            paused_at = s.pause.paused_since()
            paused_text = time.strftime("%H:%M", time.localtime(paused_at)) if paused_at else ""
            by_stage: dict[str, list[dict]] = {}
            for r in rows:
                last_code = s.agent_runs.last_exit_code(r["key"])
                flags = r["flags"]
                reason = s.tickets.last_note(r["key"], "flag")
                by_stage.setdefault(r["stage"], []).append({
                    "key": r["key"], "stage": r["stage"], "summary": r["summary"], "flag_list": flags,
                    "idle": pipeline.idle_reason(r["key"]),
                    "flag_reason": (reason or "") if flags else "",
                    "in_sprint": r["sprint_state"] == "active",
                    "role": None if flags else STAGE_ROLE.get(r["stage"]),
                    "can_merge": False,
                    "can_publish": r["stage"] == "deployed",
                    "can_deploy": r["stage"] in ("regression", "performance") and deploy.prod_ready(r["key"]),
                    "last": {"exit_code": last_code} if last_code is not None else None,
                })
        columns = []
        for st in stage_view.ordered_stages():
            columns.append({"stage": st["name"], "label": st["label"], "owner": st["owner"], "role": st["role"],
                            "tickets": by_stage.get(st["name"], [])})
        state = activity.snapshot()
        return {"columns": columns, "queued": queued, "paused": paused, "paused_text": paused_text,
                "state": state, "repos": settings.repos,
                "access": settings.claude_code_access, "test_cmd": settings.test_cmd,
                "model": settings.model, "runs_dir": str(settings.runs_dir)}

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):
        return page(request, **context())

    @app.get("/agents/board", response_class=HTMLResponse)
    def board_partial(request: Request):
        return templates.TemplateResponse(request, "_board.html", context())

    @app.post("/agents/pause")
    def pause_work(request: Request):
        with container.session() as s:
            changed = s.pause.pause("paused by a person", int(container.clock()))
        _log("work paused: no new run or stage move starts; a run already in progress finishes"
             if changed else "work was already paused")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/resume")
    def resume_work(request: Request):
        with container.session() as s:
            changed = s.pause.resume(int(container.clock()))
        _log("work resumed" if changed else "work was not paused")
        _wake.set()  # the auto loop picks the board up right away
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/tick")
    def run_pass(request: Request):
        settings = container.settings()
        with container.session() as s:
            paused = s.pause.is_paused()
        if paused:
            _log("work is paused: resume it before starting a pass")
        elif not settings.repos:
            _log("no repo is linked: add one on the Links page first")
        elif not _start("one pass", container.background_work(settings).run_pass):
            _log("a pass is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/webhooks/github")
    async def github_hook(request: Request):
        """GitHub tells us something changed: CI finished, a PR moved. We check the forge now."""
        secret = os.environ.get("WF_GITHUB_WEBHOOK_SECRET", "")
        body = await request.body()
        if not github_webhook.signature_ok(secret, body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(403, "bad signature")
        event = request.headers.get("X-GitHub-Event", "")
        if github_webhook.is_relevant(event):
            _wake.set()
        return {"ok": True, "event": event}

    def auto_loop():
        """Start the next pass by itself when a ticket is ready, e.g. right after its flag clears."""
        while True:
            _wake.wait(15)  # a webhook wakes it early; otherwise it checks every 15 seconds
            _wake.clear()
            try:
                settings = container.settings()
                jira_changed = _jira_changed.is_set()
                with container.session() as s:
                    waiting = s.cycle(settings, activity.Activity()).run_once(jira_changed=jira_changed)
                if jira_changed and not waiting:
                    _jira_changed.clear()  # the sync started; otherwise it stays set for the next cycle
            except Exception as e:  # never let the loop die
                _log(f"auto pass check failed: {e}")

    threading.Thread(target=auto_loop, daemon=True).start()

    def _ticket_url(key: str, notice: str) -> str:
        return f"/board/ticket/{key}?notice={quote(notice)}"

    @app.post("/agents/run/{key}")
    def run_one(key: str):
        settings = container.settings()
        with container.session() as s:
            try:
                plan = s.run_ticket(settings).prepare(key)
            except NotStarted as e:
                _log(f"{key}: {e}")
                return RedirectResponse(_ticket_url(key, str(e)), status_code=303)

        def work():
            with container.session() as s:
                exit_code = s.run_ticket(settings).execute(plan)
            _log(f"{key}: {plan.role} finished (exit {exit_code})")

        if not _start(f"{key} {plan.role}", work):
            notice = "Not started: another action is already running. Try again when it finishes."
        else:
            notice = f"Started the {plan.role} agent on {key}. The result appears below and in the Activity log."
        _log(f"{key}: {notice}")
        return RedirectResponse(_ticket_url(key, notice), status_code=303)

    def _render(name: str, **ctx) -> str:
        return templates.get_template(name).render(**ctx)

    def _ticket_live_html(key: str) -> str:
        settings = container.settings()
        with container.session() as s:
            idle = s.pipeline(settings).idle_reason(key)
        running = activity.snapshot()["running"]
        mine = running if running and running.startswith(key + " ") else None
        other = running if running and not mine else None
        return _render("_ticket_live.html", ticket={"key": key}, idle=idle, running=mine, other=other)

    @app.get("/events")
    def events(ticket: str = ""):
        """One server-sent stream for the page. It sends only what changed since the last message.

        The browser opens it once and never polls. The server checks the board about every
        second and pushes: a card that changed, a card that left, a column header, the activity
        panel, and (on a ticket page) that ticket's live status.
        """
        def frames():
            prev_cards: dict[str, tuple[str, str]] = {}
            prev_cols: dict[str, tuple[bool, str]] = {}
            prev_state = None
            prev_live = None
            beats = 0
            while True:
                msgs: list[dict] = []
                try:
                    ctx = context()
                    cols: dict[str, tuple[bool, str]] = {}
                    cards: dict[str, tuple[str, str]] = {}
                    for col in ctx["columns"]:
                        stage = col["stage"]
                        cols[stage] = (bool(col["tickets"]), _render("_col_meta.html", col=col))
                        for t in col["tickets"]:
                            cards[t["key"]] = (stage, _render("_card.html", t=t, stage=stage, state=ctx["state"]))
                    msgs.extend(board_events(prev_cards, prev_cols, cards, cols))
                    state_html = _render("_agents_state.html", state=ctx["state"])
                    if state_html != prev_state:
                        msgs.append({"type": "state", "html": state_html})
                        prev_state = state_html
                    if ticket:
                        live = _ticket_live_html(ticket)
                        if live != prev_live:
                            msgs.append({"type": "live", "html": live})
                            prev_live = live
                    prev_cards, prev_cols = cards, cols
                except Exception as e:  # keep the stream alive; the next pass tries again
                    msgs.append({"type": "error", "message": str(e)})
                for m in msgs:
                    yield f"data: {json.dumps(m)}\n\n"
                beats += 1
                if beats % 15 == 0:
                    yield ": keep-alive\n\n"
                time.sleep(1.0)

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/agents/deploy/{key}")
    def deploy_ticket(key: str, confirm: str = Form("")):
        """The one manual step: a person deploys a ticket that has passed regression."""
        if confirm != "yes":
            _log(f"{key}: deploy to prod needs the confirm box ticked")
            return RedirectResponse("/agents", status_code=303)
        settings = container.settings()
        try:
            with container.session() as s:
                s.deploy(settings).deploy_to_prod(key, settings.repo_slug())
            _log(f"{key} deployed to prod (by you)")
        except BoardError as e:
            _log(f"{key}: deploy refused: {e}")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/merge/{key}")
    def merge_ticket(key: str):
        settings = container.settings()

        def work():
            with container.session() as s:
                head = s.merge(settings).merge(key, settings.test_cmd)
            _log(f"{key} merged into main at {head[:8]}")

        if not _start(f"merge {key}", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/publish/{key}")
    def publish_ticket(key: str, confirm: str = Form("")):
        settings = container.settings()
        if confirm != "yes":
            _log(f"{key}: publish needs the confirm box ticked")
            return RedirectResponse("/agents", status_code=303)

        def work():
            with container.session() as s:
                url = s.publish(settings).open_pull_request(key, confirm=True)
            _log(f"{key} pull request: {url}")

        if not _start(f"publish {key}", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/jira/sync")
    def jira_sync(next: str = Form("/agents")):
        settings = container.settings()
        if not _start("jira sync", container.background_work(settings).jira_sync):
            _log("another action is already running")
        back = next if next in ("/agents", "/board/backlog") else "/agents"
        return RedirectResponse(back, status_code=303)

    @app.middleware("http")
    async def tunnel_only_webhooks(request: Request, call_next):
        """A public tunnel reaches the webhook receivers only. Local pages stay local."""
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        tunneled = "x-forwarded-for" in request.headers or host.endswith((".ngrok-free.app", ".ngrok.app", ".ngrok.io"))
        if tunneled and not request.url.path.startswith("/webhooks/"):
            return PlainTextResponse("not found", status_code=404)
        return await call_next(request)

    @app.post("/webhooks/jira/{token}")
    async def jira_hook(token: str, request: Request):
        """Jira says a ticket or sprint changed. We read Jira again now."""
        expected = os.environ.get("WF_JIRA_WEBHOOK_TOKEN", "")
        if not jira_webhook.token_ok(expected, token):
            raise HTTPException(403, "bad token")
        try:
            event = (await request.json()).get("webhookEvent")
        except Exception:
            event = None
        if jira_webhook.is_relevant(event):
            _jira_changed.set()
            _wake.set()
        return {"ok": True, "event": event}

    def _active_sprint():
        with container.session() as s:
            return s.queries.active_sprint()

    @app.post("/board/hopper/to-sprint")
    def hopper_to_sprint(keys: list[str] = Form(default=[])):
        """Move the tickets the browser sent into the active sprint, in one Jira write. Nothing is stored here."""
        import re as _re
        keys = [k for k in keys if _re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", k)]
        settings = container.settings()
        sprint = _active_sprint()
        if sprint is None or not keys:
            _log("hopper: nothing to move, or no active sprint")
            return RedirectResponse("/board/backlog", status_code=303)

        def work():
            with container.session() as s:
                s.jira(settings).move_tickets_to_sprint(keys, sprint)

        if not _start(f"move {len(keys)} ticket(s) to sprint", work):
            _log("another action is already running")
        return RedirectResponse("/board/backlog?sent=1", status_code=303)

    @app.post("/board/item/{key}/to-sprint")
    def move_to_sprint(key: str):
        """The user asks for one of their tickets to join the active sprint. Jira is written, then read back."""
        import re as _re
        if not _re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
            raise HTTPException(400, "bad ticket key")
        settings = container.settings()
        sprint = _active_sprint()
        if sprint is None:
            _log(f"{key}: no active sprint to move into")
            return RedirectResponse(f"/board/item/{key}", status_code=303)

        def work():
            with container.session() as s:
                s.jira(settings).move_to_sprint(key, sprint)

        if not _start(f"move {key} to sprint", work):
            _log("another action is already running")
        return RedirectResponse(f"/board/item/{key}", status_code=303)

    @app.post("/agents/jira/send")
    def jira_send(confirm: str = Form("")):
        settings = container.settings()
        if confirm != "yes":
            _log("sending to Jira needs the confirm box ticked")
            return RedirectResponse("/agents", status_code=303)

        def work():
            with container.session() as s:
                s.jira(settings).send_queued()

        if not _start("send to Jira", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.get("/board/ticket/{key}/pr-card", response_class=HTMLResponse)
    def pr_card(request: Request, key: str):
        """The internal pull request for a ticket: its branch, head commit, approvals, and diff."""
        settings = container.settings()
        with container.session() as s:
            if s.tickets.get(key) is None:
                return HTMLResponse("", status_code=404)
            branch = branch_for(key)
            if not settings.repos:
                return HTMLResponse('<div class="muted">No repo linked.</div>')
            workspace = s.workspace(settings)
            head = workspace.branch_head(key)
            if head is None:
                return HTMLResponse('<div class="muted">No branch yet. The dev run creates it.</div>')
            stat = workspace.diff_stat(key)
            pr = s.tickets.last_note(key, "pr")
            approvals = sorted(s.tickets.approvals_at(key, head))
            tests = s.agent_runs.last_exit_code(key, "dev")
            verdict = s.merge_bot(settings).assess(key)
            merged = bool(pr) and s.merge_bot(settings).is_merged(pr)
        summary = _html.escape(stat[-1]) if stat else "no changes against main"
        if merged:
            verdict_html = '<div class="item"><b class="ok">Merged to GitHub main</b></div>'
        else:
            verdict_html = ('<div class="item"><b class="ok">Merge bot: good to merge</b></div>' if verdict.ok else
                            '<div class="item"><b class="bad">Merge bot: not good to merge</b><ul>'
                            + "".join(f"<li>{_html.escape(r)}</li>" for r in verdict.reasons) + "</ul></div>")
        approved = ", ".join(approvals) if approvals else "none at this commit"
        body = f"""<div class="panel"><div class="row between"><b>{_html.escape(branch)}</b>
<span class="muted">{head[:10]}</span></div>
<div class="item">Changes: {summary}</div>
{verdict_html}
<div class="item">Approved at this commit: {_html.escape(approved)}</div>
<div class="item">Last dev run: {"ok" if tests == 0 else "not ok" if tests is not None else "none"}</div>
<div class="item"><a href="/board/ticket/{_html.escape(key)}/diff" target="_blank">View the diff</a></div>
{('<div class="item">GitHub PR: <a href="' + _html.escape(pr) + '" target="_blank">' + _html.escape(pr) + '</a></div>') if pr else ''}</div>"""
        return HTMLResponse(body)

    @app.get("/board/ticket/{key}/diff", response_class=PlainTextResponse)
    def diff(key: str):
        settings = container.settings()
        with container.session() as s:
            return PlainTextResponse(s.workspace(settings).diff_text(key) or "no diff")

    @app.get("/agents/state", response_class=HTMLResponse)
    def state_partial(request: Request):
        return templates.TemplateResponse(request, "_agents_state.html", {"state": context()["state"]})


def board_events(prev_cards: dict, prev_cols: dict, cards: dict, cols: dict) -> list[dict]:
    """The events that turn the last board into the current one. Only what changed is sent.

    cards: key -> (stage, card html). cols: stage -> (has tickets, column header html).
    """
    msgs: list[dict] = []
    for stage, value in cols.items():
        if prev_cols.get(stage) != value:
            msgs.append({"type": "column.update", "stage": stage, "has": value[0], "html": value[1]})
    for key, (stage, html) in cards.items():
        if key not in prev_cards:
            msgs.append({"type": "card.add", "key": key, "stage": stage, "html": html})
            continue
        was_stage, was_html = prev_cards[key]
        if was_stage != stage:
            msgs.append({"type": "card.move", "key": key, "from": was_stage, "to": stage, "html": html})
        elif was_html != html:
            msgs.append({"type": "card.update", "key": key, "html": html})
    for key in prev_cards.keys() - cards.keys():
        msgs.append({"type": "card.remove", "key": key})
    return msgs
