"""Rendering: a view model and its template make a response."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from wf.entrypoints.web.viewmodels.base import ViewModel

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def render(request: Request, vm: ViewModel, status_code: int = 200) -> HTMLResponse:
    """The view model bound to its template."""
    return templates.TemplateResponse(request, vm.template, vm.bindings(), status_code=status_code)


def render_to_string(vm: ViewModel) -> str:
    """The same, as text: for the live stream, which sends fragments."""
    return templates.get_template(vm.template).render(**vm.bindings())


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def go(request: Request, url: str) -> Response:
    """htmx follows HX-Redirect itself; a plain form post gets a normal redirect."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": url})
    return redirect(url)
