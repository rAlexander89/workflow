"""The workflow runs: the list, one run, and its decision to approve or reject."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from wf.entrypoints.web.viewmodels.base import ViewModel


@dataclass
class RunsViewModel(ViewModel):
    template = "runs.html"
    active = "runs"
    runs: list = field(default_factory=list)

    @classmethod
    def load(cls, container) -> "RunsViewModel":
        return cls(runs=container.runs().list())


@dataclass
class RunStatusViewModel(ViewModel):
    """A run's status. It is a page fragment: deciding replaces it in place."""
    template = "_run_status.html"
    run: object = None
    snap: dict = field(default_factory=dict)

    @classmethod
    def load(cls, container, run_id: str) -> "RunStatusViewModel | None":
        run = container.runs().get(run_id)
        return None if run is None else cls(run=run, snap=container.workflows().snapshot(run_id))

    @classmethod
    def decide(cls, container, run_id: str, approved: bool) -> "RunStatusViewModel | None":
        """Approve or reject a run that is waiting for it. A run that is not waiting is left as it is."""
        runs, workflows = container.runs(), container.workflows()
        run = runs.get(run_id)
        if run is None:
            return None
        snap = workflows.snapshot(run_id)
        if snap["status"] == "waiting_for_approval":
            snap = workflows.resume(run_id, approved=approved)
            runs.set_status(run_id, snap["status"])
            run = runs.get(run_id)
        return cls(run=run, snap=snap)


@dataclass
class RunViewModel(RunStatusViewModel):
    template = "run.html"
    active = "runs"


def start_run(container, brief: str) -> str:
    """Start a run from a request. Returns its id."""
    run_id = uuid.uuid4().hex
    container.runs().create(run_id, brief)
    out = container.workflows().start(run_id, brief)
    container.runs().set_status(run_id, out["status"])
    return run_id
