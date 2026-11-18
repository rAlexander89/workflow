"""Routes for the pipeline: the agents page, its live stream, the actions, and the webhooks.

A route creates a view model and renders it, or calls a command on `PipelineCommands`. What the screens show
and do lives in the view models; this module is HTTP, the live stream, and the loop that wakes the pipeline.
"""

from __future__ import annotations

import json
import os
import threading
import time

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from wf.bootstrap import Container
from wf.domain.delivery import github_webhook
from wf.domain.shared.ticket_key import TicketKey
from wf.domain.tracker import jira_webhook
from wf.entrypoints.web import activity
from wf.entrypoints.web.rendering import redirect, render, render_to_string, templates  # noqa: F401
from wf.entrypoints.web.viewmodels.pipeline import (
    ActivityViewModel, PipelineBoardViewModel, PipelineCommands, PipelineViewModel, TicketLiveViewModel, board_events,
)
from wf.entrypoints.web.viewmodels.pr_card import PrCardViewModel

_wake = threading.Event()  # set by a relevant GitHub webhook: the auto loop checks now, not in 15s
_jira_changed = threading.Event()  # set by a Jira webhook: the auto loop reads Jira again now
_log = activity.log
_start = activity.start


def mount_agents(app: FastAPI, container: Container) -> None:
    commands = PipelineCommands(container)

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):
        return render(request, PipelineViewModel.load(container))

    @app.get("/agents/board", response_class=HTMLResponse)
    def board_partial(request: Request):
        return render(request, PipelineBoardViewModel.load(container))

    @app.get("/agents/state", response_class=HTMLResponse)
    def state_partial(request: Request):
        return render(request, ActivityViewModel.load())

    # --- actions -----------------------------------------------------------------------

    @app.post("/agents/pause")
    def pause_work():
        commands.pause()
        return redirect("/agents")

    @app.post("/agents/resume")
    def resume_work():
        commands.resume()
        _wake.set()  # the auto loop picks the board up right away
        return redirect("/agents")

    @app.post("/agents/tick")
    def run_pass():
        commands.run_pass()
        return redirect("/agents")

    @app.post("/agents/run/{key}")
    def run_one(key: str):
        return RedirectResponse(commands.ticket_url(key, commands.run_one(key)), status_code=303)

    @app.post("/agents/deploy/{key}")
    def deploy_ticket(key: str, confirm: str = Form("")):
        commands.deploy(key, confirm)
        return redirect("/agents")

    @app.post("/agents/merge/{key}")
    def merge_ticket(key: str):
        commands.merge(key)
        return redirect("/agents")

    @app.post("/agents/publish/{key}")
    def publish_ticket(key: str, confirm: str = Form("")):
        commands.publish(key, confirm)
        return redirect("/agents")

    @app.post("/agents/jira/sync")
    def jira_sync(next: str = Form("/agents")):
        commands.jira_sync()
        return redirect(next if next in ("/agents", "/board/backlog") else "/agents")

    @app.post("/agents/jira/send")
    def jira_send(confirm: str = Form("")):
        commands.jira_send(confirm)
        return redirect("/agents")

    @app.post("/board/hopper/to-sprint")
    def hopper_to_sprint(keys: list[str] = Form(default=[])):
        commands.move_tickets_to_sprint(keys)
        return redirect("/board/backlog?sent=1")

    @app.post("/board/item/{key}/to-sprint")
    def move_to_sprint(key: str):
        if not TicketKey.is_valid(key):
            raise HTTPException(400, "bad ticket key")
        commands.move_to_sprint(key)
        return redirect(f"/board/item/{key}")

    # --- the ticket's pull request -------------------------------------------------------

    @app.get("/board/ticket/{key}/pr-card", response_class=HTMLResponse)
    def pr_card(request: Request, key: str):
        vm = PrCardViewModel.load(container, key)
        return HTMLResponse("", status_code=404) if vm is None else render(request, vm)

    @app.get("/board/ticket/{key}/diff", response_class=PlainTextResponse)
    def diff(key: str):
        return PlainTextResponse(PrCardViewModel.diff(container, key))

    # --- the live stream --------------------------------------------------------------------

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
                    board = PipelineBoardViewModel.load(container)
                    cols: dict[str, tuple[bool, str]] = {}
                    cards: dict[str, tuple[str, str]] = {}
                    for col in board.columns:
                        cols[col.stage] = (bool(col.tickets), render_to_string(col))
                        for card in col.tickets:
                            cards[card.key] = (col.stage, render_to_string(card))
                    msgs.extend(board_events(prev_cards, prev_cols, cards, cols))
                    state_html = render_to_string(ActivityViewModel.load())
                    if state_html != prev_state:
                        msgs.append({"type": "state", "html": state_html})
                        prev_state = state_html
                    if ticket:
                        live = render_to_string(TicketLiveViewModel.load(container, ticket))
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

    # --- webhooks and the loop ------------------------------------------------------------------

    @app.middleware("http")
    async def tunnel_only_webhooks(request: Request, call_next):
        """A public tunnel reaches the webhook receivers only. Local pages stay local."""
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        tunneled = "x-forwarded-for" in request.headers or host.endswith((".ngrok-free.app", ".ngrok.app", ".ngrok.io"))
        if tunneled and not request.url.path.startswith("/webhooks/"):
            return PlainTextResponse("not found", status_code=404)
        return await call_next(request)

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
