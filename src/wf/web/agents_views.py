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

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from wf import agents, board
from wf import config as cfgmod
from wf import claude_run, github_agent, jira_session, merge, outbox
from wf.web import pipeline as stage_view

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

_lock = threading.Lock()
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
            by_stage: dict[str, list[dict]] = {}
            for r in rows:
                last = conn.execute(
                    "select exit_code from agent_run where key = ? order by id desc limit 1", (r["key"],)).fetchone()
                flags = json.loads(r["flags"] or "[]")
                reason = conn.execute(
                    "select detail from ticket_event where ticket_key = ? and kind = 'flag' "
                    "order by id desc limit 1", (r["key"],)).fetchone()
                by_stage.setdefault(r["stage"], []).append({
                    "key": r["key"], "summary": r["summary"], "flag_list": flags,
                    "flag_reason": (reason["detail"] or "") if reason and flags else "",
                    "in_sprint": r["sprint_state"] == "active",
                    "role": None if flags else agents.STAGE_ROLE.get(r["stage"]),
                    "can_merge": r["stage"] == "qa_approved",
                    "can_publish": r["stage"] == "deployed",
                    "last": {"exit_code": last["exit_code"]} if last else None,
                })
        finally:
            conn.close()
        columns = []
        for st in stage_view.ordered_stages():
            columns.append({"label": st["label"], "owner": st["owner"], "role": st["role"],
                            "tickets": by_stage.get(st["name"], [])})
        with _lock:
            state = {"running": _state["running"], "last": _state["last"], "log": list(_state["log"])}
        return {"columns": columns, "queued": queued, "state": state, "repos": c.repos,
                "access": c.model.claude_code_access, "test_cmd": c.agents.test_cmd,
                "model": c.agents.model, "runs_dir": str(_runs_dir(c))}

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):
        return page(request, **context())

    @app.post("/agents/tick")
    def run_pass(request: Request):
        c = settings()
        access = c.model.claude_code_access
        repos = c.repos

        def work():
            conn = board.connect(db_path)
            try:
                actions = agents.tick(
                    conn, repo=_repo_path(c, None), runs_dir=_runs_dir(c), access=access,
                    model=c.agents.model, test_cmd=c.agents.test_cmd, app_cmd=c.agents.app_cmd, dry_run=False,
                    claude_bin=_claude_bin(),
                )
                for a in actions:
                    _log(f"{a['key']}: {a['role']} on {a['stage']}"
                         + (f" -> {a['skipped']}" if a.get("skipped") else f" (exit {a.get('exit_code')})"))
                if not actions:
                    _log("nothing to run: no ticket is waiting for an agent")
            finally:
                conn.close()

        if not repos:
            _log("no repo is linked: add one on the Links page first")
        elif not _start("one pass", work):
            _log("a pass is already running")
        return RedirectResponse("/agents", status_code=303)

    @app.post("/agents/run/{key}")
    def run_one(key: str):
        c = settings()

        def work():
            conn = board.connect(db_path)
            try:
                stage = conn.execute("select stage from ticket where key = ?", (key,)).fetchone()
                if stage is None or stage[0] not in agents.STAGE_ROLE:
                    raise board.BoardError(f"{key} has no agent for its stage")
                role = agents.STAGE_ROLE[stage[0]]
                if stage[0] == "ready_for_dev":
                    board.move_stage(conn, key, "in_dev", "dev", "dev pickup")
                res = agents.run_agent(conn, role, key, repo=_repo_path(c, None), runs_dir=_runs_dir(c),
                                       access=c.model.claude_code_access, model=c.agents.model,
                                       test_cmd=c.agents.test_cmd, app_cmd=c.agents.app_cmd, claude_bin=_claude_bin())
                code = res.run.exit_code if res.run else None
                _log(f"{key}: {role} finished (exit {code})")
            finally:
                conn.close()

        if not _start(f"{key} {agents.STAGE_ROLE.get('ready_for_dev', 'agent')}", work):
            _log("another action is already running")
        return RedirectResponse(f"/board/ticket/{key}", status_code=303)

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

    @app.post("/agents/jira/sync")
    def jira_sync():
        c = settings()

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

        if not _start("jira sync", work):
            _log("another action is already running")
        return RedirectResponse("/agents", status_code=303)

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

    @app.get("/agents/state", response_class=HTMLResponse)
    def state_partial(request: Request):
        return templates.TemplateResponse(request, "_agents_state.html", {"state": context()["state"]})


def _claude_bin() -> str | None:
    import shutil

    return shutil.which(claude_run.CLAUDE_BIN)
