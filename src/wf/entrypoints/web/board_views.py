"""Routes for the board pages. Each one creates a view model and renders it; the logic is in the view models."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from wf.bootstrap import Container
from wf.entrypoints.web.rendering import redirect, render
from wf.entrypoints.web.viewmodels.board import (
    BacklogViewModel, BoardViewModel, DeployPanelViewModel, ItemViewModel, OutboxViewModel,
)
from wf.entrypoints.web.viewmodels.ticket import TicketViewModel


def mount_board(app: FastAPI, container: Container) -> None:
    @app.get("/board", response_class=HTMLResponse)
    def board_page(request: Request):
        return render(request, BoardViewModel.load(container))

    @app.get("/board/backlog", response_class=HTMLResponse)
    def backlog_page(request: Request, sent: int = 0):
        return render(request, BacklogViewModel.load(container, sent=bool(sent)))

    @app.get("/board/item/{key}", response_class=HTMLResponse)
    def item_page(request: Request, key: str):
        vm = ItemViewModel.load(container, key)
        return HTMLResponse("No such ticket.", status_code=404) if vm is None else render(request, vm)

    @app.get("/board/ticket/{key}", response_class=HTMLResponse)
    def ticket_page(request: Request, key: str, notice: str = ""):
        vm = TicketViewModel.load(container, key, notice)
        return HTMLResponse("No such ticket.", status_code=404) if vm is None else render(request, vm)

    @app.get("/board/ticket/{key}/deploy-panel", response_class=HTMLResponse)
    def deploy_panel(request: Request, key: str):
        return render(request, DeployPanelViewModel.load(container, key))

    @app.post("/board/ticket/{key}/send-back")
    def send_back(key: str, to: str = Form(...), reason: str = Form(...)):
        TicketViewModel.send_back(container, key, to, reason)
        return redirect(f"/board/ticket/{key}")

    @app.post("/board/ticket/{key}/answer")
    def answer_flag(key: str, answer: str = Form("")):
        TicketViewModel.answer(container, key, answer)
        return redirect(f"/board/ticket/{key}")

    @app.post("/board/ticket/{key}/flag/clear")
    def clear_flag(key: str, flag: str = Form(...)):
        TicketViewModel.clear_flag(container, key, flag)
        return redirect(f"/board/ticket/{key}")

    @app.get("/board/outbox", response_class=HTMLResponse)
    def outbox_page(request: Request):
        return render(request, OutboxViewModel.load(container))

    @app.post("/board/outbox/{item_id}/retry")
    def retry_item(item_id: int):
        OutboxViewModel.retry(container, item_id)
        return redirect("/board/outbox")
