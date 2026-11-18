"""Web app: links (internal repos and Jira boards), workflow runs, the board, and the agents pipeline.

Pages are server-rendered from view models. htmx swaps fragments for form posts; Alpine handles local UI
state. Config comes from WF_CONFIG and the SQLite file from WF_DB.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from wf.application.ports.tracker import IssueReader, TrackerError
from wf.bootstrap import Container
from wf.domain.tracker.changelog import issue_key_from_webhook
from wf.entrypoints.web import activity
from wf.entrypoints.web.rendering import go, redirect, render
from wf.entrypoints.web.viewmodels.links import CheckViewModel, LinksPanelViewModel, LinksViewModel
from wf.entrypoints.web.viewmodels.runs import RunsViewModel, RunStatusViewModel, RunViewModel, start_run


def create_app(
    config_path: Path | None = None,
    db_path: Path | None = None,
    jira: IssueReader | None = None,
    webhook_token: str | None = None,
) -> FastAPI:
    config_path = Path(config_path or Container.default_config_path())
    db_path = Path(db_path or os.environ.get("WF_DB", "wf.sqlite"))
    container = Container(db_path, config_path, log=activity.Activity())
    jr = jira or container.issue_reader()
    pipeline = container.trigger_pipeline(
        fetch_changelog=lambda key: jr.get_issue_changelog(key),
        search_keys=lambda: [],  # the poll is run by `wf poll`, not by the web app
    )
    token = webhook_token if webhook_token is not None else os.environ.get("WF_WEBHOOK_TOKEN", "")

    app = FastAPI(title="wf")

    @app.get("/")
    def root():
        return redirect("/runs")

    # --- links ---------------------------------------------------------------

    @app.get("/links", response_class=HTMLResponse)
    def links_page(request: Request):
        return render(request, LinksViewModel.load(container))

    @app.post("/links/repo", response_class=HTMLResponse)
    def add_repo(request: Request, name: str = Form(...), repo_path: str = Form(...)):
        return render(request, LinksPanelViewModel.add_repo(container, name, repo_path))

    @app.post("/links/repo/remove", response_class=HTMLResponse)
    def remove_repo(request: Request, name: str = Form(...)):
        return render(request, LinksPanelViewModel.remove_repo(container, name))

    @app.post("/links/board", response_class=HTMLResponse)
    def add_board(request: Request, provider: str = Form(...), ref: str = Form(...), label: str = Form("")):
        return render(request, LinksPanelViewModel.add_board(container, provider, ref, label))

    @app.post("/links/board/remove", response_class=HTMLResponse)
    def remove_board(request: Request, provider: str = Form(...), ref: str = Form(...)):
        return render(request, LinksPanelViewModel.remove_board(container, provider, ref))

    @app.post("/links/check", response_class=HTMLResponse)
    def check_links(request: Request):
        return render(request, CheckViewModel.run(container, jr))

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
        except TrackerError as e:
            raise HTTPException(502, str(e))
        return {"routed": len(decisions), "actions": [d.action for d in decisions]}

    # --- runs ----------------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        return render(request, RunsViewModel.load(container))

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        vm = RunStatusViewModel.load(container, run_id)
        if vm is None:
            raise HTTPException(404, "no such run")
        return render(request, RunViewModel(run=vm.run, snap=vm.snap))

    @app.post("/runs")
    def start(request: Request, brief: str = Form(..., alias="request")):
        brief = brief.strip()
        if not brief:
            raise HTTPException(422, "request is empty")
        return go(request, f"/runs/{start_run(container, brief)}")

    @app.post("/runs/{run_id}/decision", response_class=HTMLResponse)
    def decide(request: Request, run_id: str, approved: str = Form(...)):
        vm = RunStatusViewModel.decide(container, run_id, approved == "yes")
        if vm is None:
            raise HTTPException(404, "no such run")
        return render(request, vm)

    from wf.entrypoints.web.board_views import mount_board

    mount_board(app, container)
    from wf.entrypoints.web.agents_views import mount_agents

    mount_agents(app, container)
    return app
