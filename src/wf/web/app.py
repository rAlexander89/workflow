"""Web app: links (internal repos and Jira boards) and workflow runs.

Pages are server-rendered. htmx swaps fragments for form posts; Alpine handles
local UI state. Config comes from WF_CONFIG and the SQLite file from WF_DB.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from wf import config as cfgmod
from wf import graph
from wf.events import issue_key_from_webhook
from wf.pipeline import Pipeline
from wf.integrations import JiraClient, JiraError, check_repo
from wf.runs import RunStore

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


@dataclass(frozen=True)
class CheckRow:
    kind: str
    name: str
    ok: bool
    detail: str


def create_app(
    config_path: Path | None = None,
    db_path: Path | None = None,
    jira: JiraClient | None = None,
    webhook_token: str | None = None,
) -> FastAPI:
    config_path = Path(config_path or cfgmod.default_path())
    db_path = Path(db_path or os.environ.get("WF_DB", "wf.sqlite"))
    jr = jira or JiraClient()
    runs = RunStore(db_path)
    pipeline = Pipeline(
        db_path,
        fetch_changelog=lambda key: jr.get_issue_changelog(key),
        search_keys=lambda: [],  # the poll is run by `wf poll`, not by the web app
    )
    token = webhook_token if webhook_token is not None else os.environ.get("WF_WEBHOOK_TOKEN", "")

    app = FastAPI(title="wf")

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def go(request: Request, url: str) -> Response:
        # htmx follows HX-Redirect itself; a plain form post gets a normal redirect.
        if is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    def links_partial(request: Request, error: str | None = None) -> HTMLResponse:
        c = cfgmod.load(config_path)
        return render(request, "_links.html", repos=c.repos, boards=c.boards, error=error)

    # --- pages ---------------------------------------------------------------

    @app.get("/")
    def root():
        return RedirectResponse("/runs", status_code=303)

    @app.get("/links", response_class=HTMLResponse)
    def links_page(request: Request):
        c = cfgmod.load(config_path)
        return render(request, "links.html", active="links", repos=c.repos, boards=c.boards, error=None)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        return render(request, "runs.html", active="runs", runs=runs.list())

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        snap = graph.snapshot(db_path, run_id)
        return render(request, "run.html", active="runs", run=run, snap=snap)

    # --- links ---------------------------------------------------------------

    @app.post("/links/repo", response_class=HTMLResponse)
    def add_repo(request: Request, name: str = Form(...), repo_path: str = Form(...)):
        c = cfgmod.load(config_path)
        error = None
        try:
            c.add_repo(name, repo_path)
            cfgmod.save(c, config_path)
        except cfgmod.ConfigError as e:
            error = str(e)
        return links_partial(request, error)

    @app.post("/links/repo/remove", response_class=HTMLResponse)
    def remove_repo(request: Request, name: str = Form(...)):
        c = cfgmod.load(config_path)
        c.remove_repo(name)
        cfgmod.save(c, config_path)
        return links_partial(request)

    @app.post("/links/board", response_class=HTMLResponse)
    def add_board(
        request: Request,
        provider: str = Form(...),
        ref: str = Form(...),
        label: str = Form(""),
    ):
        c = cfgmod.load(config_path)
        error = None
        try:
            c.add_board(provider, ref, label.strip())
            cfgmod.save(c, config_path)
        except cfgmod.ConfigError as e:
            error = str(e)
        return links_partial(request, error)

    @app.post("/links/board/remove", response_class=HTMLResponse)
    def remove_board(request: Request, provider: str = Form(...), ref: str = Form(...)):
        c = cfgmod.load(config_path)
        c.remove_board(provider, ref)
        cfgmod.save(c, config_path)
        return links_partial(request)

    @app.post("/links/check", response_class=HTMLResponse)
    def check_links(request: Request):
        c = cfgmod.load(config_path)
        rows: list[CheckRow] = []
        for r in c.repos:
            res = check_repo(r)
            rows.append(CheckRow("repo", r.name, res.ok, res.detail))
        for b in c.boards:
            res = jr.check_board(b)
            rows.append(CheckRow(f"board ({b.provider})", b.ref, res.ok, res.detail))
        return render(request, "_check.html", rows=rows)

    # --- jira webhook --------------------------------------------------------

    @app.post("/webhooks/jira")
    async def jira_webhook(request: Request, token_param: str = ""):
        # Fails closed: with no token configured, every request is refused.
        if not token or token_param != token:
            raise HTTPException(403, "bad token")
        payload = await request.json()
        key = issue_key_from_webhook(payload)
        if key is None:
            pipeline.changes.mark_webhook()
            return {"routed": 0}
        try:
            decisions = pipeline.on_webhook(key)
        except JiraError as e:
            raise HTTPException(502, str(e))
        return {"routed": len(decisions), "actions": [d.action for d in decisions]}

    # --- runs ----------------------------------------------------------------

    @app.post("/runs")
    def start_run(request: Request, brief: str = Form(..., alias="request")):
        brief = brief.strip()
        if not brief:
            raise HTTPException(422, "request is empty")
        run_id = uuid.uuid4().hex
        runs.create(run_id, brief)
        out = graph.start(db_path, run_id, brief)
        runs.set_status(run_id, out["status"])
        return go(request, f"/runs/{run_id}")

    @app.post("/runs/{run_id}/decision", response_class=HTMLResponse)
    def decide(request: Request, run_id: str, approved: str = Form(...)):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        snap = graph.snapshot(db_path, run_id)
        if snap["status"] == "waiting_for_approval":
            out = graph.resume(db_path, run_id, approved=(approved == "yes"))
            runs.set_status(run_id, out["status"])
            snap = out
            run = runs.get(run_id)
        return render(request, "_run_status.html", run=run, snap=snap)

    from wf.web.board_views import mount_board

    mount_board(app, db_path)
    return app
