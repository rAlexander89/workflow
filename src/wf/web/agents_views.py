"""Agents page: start the agents, merge, and publish from the browser.

Each action runs in a background thread, because an agent run takes minutes. The page
shows what is running, the last result, and each ticket's next step. The same functions
back the `wf agents` commands, so the UI and the CLI can't disagree.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from wf import agents, board, github_webhook, jira_webhook, pause
from wf import config as cfgmod
from wf import claude_run, github_agent, jira_session, merge, outbox
from wf.web import pipeline as stage_view

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

_lock = threading.Lock()
_wake = threading.Event()  # set by a relevant GitHub webhook: the auto loop checks now, not in 15s
_jira_changed = threading.Event()  # set by a Jira webhook: the auto loop reads Jira again now
_state: dict = {"running": None, "log": [], "last": None}


def _log(msg: str) -> None:
    with _lock:
        _state["log"] = (_state["log"] + [f"{time.strftime('%H:%M:%S')}  {msg}"])[-30:]


def _start(label: str, fn) -> bool:
    """Run one action at a time in the background. Returns False if one is already running."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = label
    _log(f"started: {label}")

    def work():
        try:
            fn()
            with _lock:
                _state["last"] = f"{label}: done"
            _log(f"finished: {label}")
        except Exception as e:  # shown on the page; the thread must not die silently
            with _lock:
                _state["last"] = f"{label}: failed: {e}"
            _log(f"failed: {label}: {e}")
            _log(traceback.format_exc(limit=2).strip().splitlines()[-1])
        finally:
            with _lock:
                _state["running"] = None

    threading.Thread(target=work, daemon=True).start()
    return True


def _repo_path(c, name: str | None) -> Path:
    repos = {r.name: r for r in c.repos}
    if not repos:
        raise board.BoardError("no internal repo is linked; add one on the Links page")
    if name is None:
        if len(repos) > 1:
            raise board.BoardError("several repos are linked; this page supports one")
        name = next(iter(repos))
    return Path(repos[name].path)


def _link(c):
    return c.repos[0] if c.repos else None


def _forge_for(c):
    """The forge the linked repo is hosted on: GitHub, or GitLab at its host."""
    from wf import forge as _forge
    link = _link(c)
    if link is None or link.forge == "github":
        return _forge.GitHub()
    if link.forge == "gitlab":
        return _forge.GitLab(host=link.host)
    return None


def _repo_slug(c) -> str:
    repos = c.repos
    if not repos or not repos[0].remote:
        raise board.BoardError("the linked repo has no GitHub remote (org/repo) set")
    return repos[0].remote


def _runs_dir(c) -> Path:
    return Path(os.path.expanduser(c.agents.runs_dir)).resolve()


def mount_agents(app: FastAPI, db_path: Path, config_path: Path) -> None:
    db_path = Path(db_path)
    config_path = Path(config_path)

    def page(request: Request, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, "agents.html", {"active": "agents", **ctx})

    def settings():
        c = cfgmod.load(config_path)
        return c

    def context() -> dict:
        c = settings()
        conn = board.connect(db_path)
        agents.ensure_schema(conn)
        try:
            rows = conn.execute(
                """select t.key, t.summary, t.stage, t.flags, s.state as sprint_state
                     from ticket t left join sprint s on s.id = t.sprint_id
                    where t.removed_at is null and t.stage is not null
                    order by t.key""").fetchall()
            queued = conn.execute("select count(*) from outbox where kind='transition' and status='pending'").fetchone()[0]
            paused = pause.is_paused(conn)
            paused_at = pause.paused_since(conn)
            paused_text = time.strftime("%H:%M", time.localtime(paused_at)) if paused_at else ""
            by_stage: dict[str, list[dict]] = {}
            for r in rows:
                last = conn.execute(
                    "select exit_code from agent_run where key = ? order by id desc limit 1", (r["key"],)).fetchone()
                flags = json.loads(r["flags"] or "[]")
                reason = conn.execute(
                    "select detail from ticket_event where ticket_key = ? and kind = 'flag' "
                    "order by id desc limit 1", (r["key"],)).fetchone()
                idle = agents.idle_reason(conn, r["key"])
                by_stage.setdefault(r["stage"], []).append({
                    "key": r["key"], "stage": r["stage"], "summary": r["summary"], "flag_list": flags,
                    "idle": idle,
                    "flag_reason": (reason["detail"] or "") if reason and flags else "",
                    "in_sprint": r["sprint_state"] == "active",
                    "role": None if flags else agents.STAGE_ROLE.get(r["stage"]),
                    "can_merge": False,
                    "can_publish": r["stage"] == "deployed",
                    "can_deploy": r["stage"] in ("regression", "performance") and agents.prod_ready(conn, r["key"], _repo_path(c, None)),
                    "last": {"exit_code": last["exit_code"]} if last else None,
                })
        finally:
            conn.close()
        columns = []
        for st in stage_view.ordered_stages():
            columns.append({"stage": st["name"], "label": st["label"], "owner": st["owner"], "role": st["role"],
                            "tickets": by_stage.get(st["name"], [])})
        with _lock:
            state = {"running": _state["running"], "last": _state["last"], "log": list(_state["log"])}
        return {"columns": columns, "queued": queued, "paused": paused, "paused_text": paused_text,
                "state": state, "repos": c.repos,
                "access": c.model.claude_code_access, "test_cmd": c.agents.test_cmd,
                "model": c.agents.model, "runs_dir": str(_runs_dir(c))}

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):
        return page(request, **context())

    @app.get("/agents/board", response_class=HTMLResponse)
    def board_partial(request: Request):
        return templates.TemplateResponse(request, "_board.html", context())

    def pass_work(c):
        access = c.model.claude_code_access

        def work():
            conn = board.connect(db_path)
            try:
                for key in agents.advance_pipeline(conn, repo=_repo_path(c, None)):
                    _log(f"{key}: moved on by the harness")
                actions = agents.tick(
                    conn, repo=_repo_path(c, None), runs_dir=_runs_dir(c), access=access,
                    model=c.agents.model, test_cmd=c.agents.test_cmd, app_cmd=c.agents.app_cmd,
                    app_url=c.agents.app_url, regression_url=c.agents.regression_url,
                    bot_domain=c.bots.domain if c.bots.enabled else "",
                    bot_credentials=c.bots.credentials_file, dry_run=False, claude_bin=_claude_bin(),
                )
                for a in actions:
                    _log(f"{a['key']}: {a['role']} on {a['stage']}"
                         + (f" -> {a['skipped']}" if a.get("skipped") else f" (exit {a.get('exit_code')})"))
                if not actions:
                    _log("nothing to run: no ticket is waiting for an agent")
            finally:
                conn.close()
        return work

    def ready_work(c) -> bool:
        conn = board.connect(db_path)
        try:
            actions = agents.tick(conn, repo=_repo_path(c, None), runs_dir=_runs_dir(c),
                                  access=c.model.claude_code_access, model=c.agents.model, dry_run=True)
        except Exception:
            return False
        finally:
            conn.close()
        return any(not a.get("skipped") for a in actions)

    @app.post("/agents/pause")
    def pause_work(request: Request):
        conn = board.connect(db_path)
        try:
            changed = pause.pause(conn)
        finally:
            conn.close()
        _log("work paused: no new run or stage move starts; a run already in progress finishes"
             if changed else "work was already paused")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/resume")
    def resume_work(request: Request):
        conn = board.connect(db_path)
        try:
            changed = pause.resume(conn)
        finally:
            conn.close()
        _log("work resumed" if changed else "work was not paused")
        _wake.set()  # the auto loop picks the board up right away
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/tick")
    def run_pass(request: Request):
        c = settings()
        conn = board.connect(db_path)
        try:
            paused = pause.is_paused(conn)
        finally:
            conn.close()
        if paused:
            _log("work is paused: resume it before starting a pass")
        elif not c.repos:
            _log("no repo is linked: add one on the Links page first")
        elif not _start("one pass", pass_work(c)):
            _log("a pass is already running")
        return RedirectResponse("/agents", status_code=303)

    def observe_tick(c):
        """Watch the merge and the dev and prod deploys on GitHub. Runs every tick, pass or no pass."""
        from wf import observe
        conn = board.connect(db_path)
        try:
            for key in [r["key"] for r in conn.execute(
                    "select key from ticket where stage = 'merged' and removed_at is null").fetchall()]:
                if observe.latest(conn, key, "merge:") is None:
                    sha = observe.merge_sha(conn, key, _repo_slug(c), forge=_forge_for(c))
                    if sha:
                        _log(f"{key}: merged to main at {sha[:8]}")
                state = observe.observe_dev(conn, key, _repo_slug(c), forge=_forge_for(c))
                if state == "success":
                    _log(f"{key}: deployed to dev")
                elif state == "failed":
                    _log(f"{key}: dev deploy FAILED")
            for key in [r["key"] for r in conn.execute(
                    "select key from ticket where stage = 'deployed' and removed_at is null").fetchall()]:
                if observe.observe_prod(conn, key, _repo_slug(c), forge=_forge_for(c)) == "success":
                    _log(f"{key}: deployed to prod (observed)")
        finally:
            conn.close()

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
                c = settings()
                conn = board.connect(db_path)
                try:
                    if pause.is_paused(conn):
                        continue  # paused: no Jira, merge, GitHub, or agent work starts
                finally:
                    conn.close()
                if _jira_changed.is_set() and c.model.claude_code_access:
                    _jira_changed.clear()
                    if not _start("jira sync", _jira_sync_work(c)):
                        _jira_changed.set()  # busy: try again on the next pass
                if not (c.agents.auto_run and c.repos and c.model.claude_code_access):
                    continue
                with _lock:
                    if _state["running"]:
                        continue
                from wf import merge as _merge
                link = _link(c)
                if link is not None and link.forge == "local":
                    conn = board.connect(db_path)
                    try:
                        for key in _merge.local_merge_when_clear(conn, repo=_repo_path(c, None), runs_dir=_runs_dir(c),
                                                                  test_cmd=c.agents.test_cmd):
                            _log(f"{key}: merged into the local main (no hosted remote)")
                    finally:
                        conn.close()
                observe_tick(c)
                conn = board.connect(db_path)
                try:
                    pending = board.pending_links(conn)
                finally:
                    conn.close()
                if pending and c.model.claude_code_access:
                    def link_work(pairs=pending):
                        linked = jira_session.create_blocking_links(pairs, **_session_kw(c))
                        conn2 = board.connect(db_path)
                        try:
                            board.mark_linked(conn2, linked)
                        finally:
                            conn2.close()
                        _log(f"linked in Jira: {', '.join(f'{p} blocks {d}' for p, d in linked)}")
                    _start("link tickets in Jira", link_work)
                conn = board.connect(db_path)
                try:
                    moved = agents.advance_pipeline(conn, repo=_repo_path(c, None))
                finally:
                    conn.close()
                for key in moved:
                    _log(f"{key}: moved on by the harness")
                from wf import github_agent
                conn = board.connect(db_path)
                try:
                    for key in github_agent.publish_ready(conn, repo=_repo_path(c, None)):
                        _log(f"{key}: GitHub PR opened")
                    for key in github_agent.merge_when_clear(conn, repo=_repo_path(c, None)):
                        _log(f"{key}: GitHub PR merged (QA passed, checks green, nothing flagged)")
                finally:
                    conn.close()
                if ready_work(c):
                    _start("auto pass", pass_work(c))
            except Exception as e:  # never let the loop die
                _log(f"auto pass check failed: {e}")

    threading.Thread(target=auto_loop, daemon=True).start()

    def _ticket_url(key: str, notice: str) -> str:
        from urllib.parse import quote
        return f"/board/ticket/{key}?notice={quote(notice)}"

    @app.post("/agents/run/{key}")
    def run_one(key: str):
        c = settings()
        conn = board.connect(db_path)
        try:
            if pause.is_paused(conn):
                notice = "Not started: work is paused. Resume it, then run the agent."
                _log(f"{key}: {notice}")
                return RedirectResponse(_ticket_url(key, notice), status_code=303)
            idle = agents.idle_reason(conn, key)
            if idle is not None and not idle["can_run"]:
                notice = f"Not started: {idle['why']}"
                _log(f"{key}: {notice}")
                return RedirectResponse(_ticket_url(key, notice), status_code=303)
            stage = conn.execute("select stage from ticket where key = ?", (key,)).fetchone()
            if stage is None or stage[0] not in agents.STAGE_ROLE:
                notice = "Not started: no agent works this stage."
                _log(f"{key}: {notice}")
                return RedirectResponse(_ticket_url(key, notice), status_code=303)
            role = (agents.refinement_role(conn, key) if stage[0] in agents.REFINEMENT_STAGES
                    else agents.STAGE_ROLE[stage[0]])
        finally:
            conn.close()

        def work():
            conn = board.connect(db_path)
            try:
                if stage[0] == "ready_for_dev":
                    board.move_stage(conn, key, "in_dev", "dev", "dev pickup")
                res = agents.run_agent(conn, role, key, repo=_repo_path(c, None), runs_dir=_runs_dir(c),
                                       access=c.model.claude_code_access, model=c.agents.model,
                                       test_cmd=c.agents.test_cmd, app_cmd=c.agents.app_cmd, app_url=c.agents.app_url, regression_url=c.agents.regression_url, bot_domain=c.bots.domain if c.bots.enabled else "", bot_credentials=c.bots.credentials_file, claude_bin=_claude_bin())
                code = res.run.exit_code if res.run else None
                _log(f"{key}: {role} finished (exit {code})")
            finally:
                conn.close()

        if not _start(f"{key} {role}", work):
            notice = "Not started: another action is already running. Try again when it finishes."
        else:
            notice = f"Started the {role} agent on {key}. The result appears below and in the Activity log."
        _log(f"{key}: {notice}")
        return RedirectResponse(_ticket_url(key, notice), status_code=303)

    def _render(name: str, **ctx) -> str:
        return templates.get_template(name).render(**ctx)

    def _ticket_live_html(key: str) -> str:
        conn = board.connect(db_path)
        try:
            idle = agents.idle_reason(conn, key)
        finally:
            conn.close()
        with _lock:
            running = _state["running"]
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
        c = settings()
        conn = board.connect(db_path)
        try:
            if not agents.prod_ready(conn, key, _repo_path(c, None)):
                _log(f"{key}: deploy refused: regression has not passed on the current commit")
                return RedirectResponse("/agents", status_code=303)
            from wf import observe
            if not observe.start_prod_deploy(conn, key, _repo_slug(c), forge=_forge_for(c)):
                _log(f"{key}: deploy refused: the prod workflow could not be started")
                return RedirectResponse("/agents", status_code=303)
            board.move_stage(conn, key, "deployed", "user", "deployed to prod by the user")
            _log(f"{key} deployed to prod (by you)")
        except board.BoardError as e:
            _log(f"{key}: deploy refused: {e}")
        finally:
            conn.close()
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/merge/{key}")
    def merge_ticket(key: str):
        c = settings()

        def work():
            conn = board.connect(db_path)
            try:
                head = merge.merge(conn, key, repo=_repo_path(c, None), runs_dir=_runs_dir(c),
                                   test_cmd=c.agents.test_cmd)
                _log(f"{key} merged into main at {head[:8]}")
            finally:
                conn.close()

        if not _start(f"merge {key}", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/publish/{key}")
    def publish_ticket(key: str, confirm: str = Form("")):
        c = settings()
        if confirm != "yes":
            _log(f"{key}: publish needs the confirm box ticked")
            return RedirectResponse("/agents", status_code=303)

        def work():
            conn = board.connect(db_path)
            try:
                url = github_agent.open_pull_request(conn, key, repo=_repo_path(c, None), confirm=True)
                _log(f"{key} pull request: {url}")
            finally:
                conn.close()

        if not _start(f"publish {key}", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

    def _session_kw(c) -> dict:
        return {"access": c.model.claude_code_access, "workdir": _runs_dir(c) / "jira-session",
                "model": c.agents.model, "claude_bin": _claude_bin()}

    def _jira_sync_work(c):
        def work():
            payload = jira_session.fetch_sync_payload(**_session_kw(c))
            conn = board.connect(db_path)
            try:
                board.carry_workflow_statuses(conn, payload)
                result = board.apply_payload(conn, payload, clock=time.time)
            finally:
                conn.close()
            counts = result.counts
            _log(f"synced {len(payload['tickets'])} ticket(s) from Jira; "
                 f"{counts.get('tickets_created', 0)} new, {counts.get('tickets_updated', 0)} updated")
            for problem in result.problems:
                _log(f"sync: {problem}")
        return work

    @app.post("/agents/jira/sync")
    def jira_sync(next: str = Form("/agents")):
        c = settings()
        if not _start("jira sync", _jira_sync_work(c)):
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

    @app.post("/board/hopper/to-sprint")
    def hopper_to_sprint(keys: list[str] = Form(default=[])):
        """Move the tickets the browser sent into the active sprint, in one Jira write. Nothing is stored here."""
        import re as _re
        keys = [k for k in keys if _re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", k)]
        c = settings()
        conn = board.connect(db_path)
        try:
            sprint = conn.execute("select id, name from sprint where state = 'active' order by id limit 1").fetchone()
        finally:
            conn.close()
        if sprint is None or not keys:
            _log("hopper: nothing to move, or no active sprint")
            return RedirectResponse("/board/backlog", status_code=303)

        def work():
            moved = jira_session.move_tickets_to_sprint(keys, sprint["id"], **_session_kw(c))
            _log(f"moved {len(moved)} ticket(s) to {sprint['name']} in Jira: {', '.join(moved)}")
            _jira_sync_work(c)()

        if not _start(f"move {len(keys)} ticket(s) to sprint", work):
            _log("another action is already running")
        return RedirectResponse("/board/backlog?sent=1", status_code=303)

    @app.post("/board/item/{key}/to-sprint")
    def move_to_sprint(key: str):
        """The user asks for one of their tickets to join the active sprint. Jira is written, then read back."""
        import re as _re
        if not _re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
            raise HTTPException(400, "bad ticket key")
        c = settings()
        conn = board.connect(db_path)
        try:
            sprint = conn.execute("select id, name from sprint where state = 'active' order by id limit 1").fetchone()
        finally:
            conn.close()
        if sprint is None:
            _log(f"{key}: no active sprint to move into")
            return RedirectResponse(f"/board/item/{key}", status_code=303)

        def work():
            jira_session.move_to_sprint(key, sprint["id"], **_session_kw(c))
            _log(f"{key}: moved to {sprint['name']} in Jira")
            _jira_sync_work(c)()

        if not _start(f"move {key} to sprint", work):
            _log("another action is already running")
        return RedirectResponse(f"/board/item/{key}", status_code=303)

    @app.post("/agents/jira/send")
    def jira_send(confirm: str = Form("")):
        c = settings()
        if confirm != "yes":
            _log("sending to Jira needs the confirm box ticked")
            return RedirectResponse("/agents", status_code=303)

        def work():
            conn = board.connect(db_path)
            try:
                items = outbox.pending(conn)
                if not items:
                    _log("nothing queued for Jira")
                    return
                keys = sorted({i["ticket_key"] for i in items})
                observed = jira_session.fetch_observations(keys, **_session_kw(c))
                plan = outbox.plan(conn, observed)
                for skipped in plan.get("need", []):
                    _log(f"{skipped}: Jira state not read; not sent")
                sends = [{"id": i["id"], "key": i["key"], "transition_id": i["transition_id"],
                          "to_status": i["to_status"]} for i in plan["send"]]
                for s_ in sends:
                    _log(f"will move {s_['key']} to {s_['to_status']}")
                results = jira_session.execute_sends(sends, **_session_kw(c))
                if results:
                    summary = outbox.ack(conn, results)
                    _log(f"Jira: {summary}")
            finally:
                conn.close()

        if not _start("send to Jira", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.get("/board/ticket/{key}/pr-card", response_class=HTMLResponse)
    def pr_card(request: Request, key: str):
        """The internal pull request for a ticket: its branch, head commit, approvals, and diff."""
        import html as _html
        import subprocess

        c = settings()
        conn = board.connect(db_path)
        try:
            row = conn.execute("select stage from ticket where key = ?", (key,)).fetchone()
            if row is None:
                return HTMLResponse("", status_code=404)
            branch = agents.branch_for(key)
            try:
                repo = _repo_path(c, None)
            except board.BoardError:
                return HTMLResponse('<div class="muted">No repo linked.</div>')
            head_proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=repo,
                                       capture_output=True, text=True)
            if head_proc.returncode != 0:
                return HTMLResponse('<div class="muted">No branch yet. The dev run creates it.</div>')
            head = head_proc.stdout.strip()
            stat = subprocess.run(["git", "diff", "--stat", agents.diff_range(repo, key)], cwd=repo,
                                  capture_output=True, text=True).stdout.strip().splitlines()
            pr = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'pr' "
                              "order by id desc limit 1", (key,)).fetchone()
            approvals = sorted(board.approvals_at(conn, key, head))
            tests = conn.execute(
                "select exit_code from agent_run where key = ? and role = 'dev' order by id desc limit 1",
                (key,)).fetchone()
        finally:
            conn.close()
        summary = _html.escape(stat[-1]) if stat else "no changes against main"
        from wf import merge_bot as _mb
        conn2 = board.connect(db_path)
        try:
            verdict = _mb.assess(conn2, key, repo=_repo_path(c, None))
        finally:
            conn2.close()
        from wf import github_status as _ghs
        pr_row = board.connect(db_path).execute(
            "select detail from ticket_event where ticket_key = ? and kind = 'pr' order by id desc limit 1", (key,)).fetchone()
        if pr_row and _ghs.pr_status(pr_row[0]).get("merged"):
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
<div class="item">Last dev run: {"ok" if tests and tests[0] == 0 else "not ok" if tests else "none"}</div>
<div class="item"><a href="/board/ticket/{_html.escape(key)}/diff" target="_blank">View the diff</a></div>
{('<div class="item">GitHub PR: <a href="' + _html.escape(pr[0]) + '" target="_blank">' + _html.escape(pr[0]) + '</a></div>') if pr else ''}</div>"""
        return HTMLResponse(body)

    @app.get("/board/ticket/{key}/diff", response_class=PlainTextResponse)
    def diff(key: str):
        import subprocess

        c = settings()
        branch = agents.branch_for(key)
        repo = _repo_path(c, None)
        proc = subprocess.run(["git", "diff", agents.diff_range(repo, key)], cwd=repo, capture_output=True, text=True)
        return PlainTextResponse(proc.stdout or proc.stderr or "no diff")

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


def _claude_bin() -> str | None:
    import shutil

    return shutil.which(claude_run.CLAUDE_BIN)
