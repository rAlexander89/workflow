"""The ticket page: where a ticket is in the pipeline, what its agents did, and what a person can do about it."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

from wf.domain.board.errors import BoardError
from wf.domain.delivery.roles import STAGE_ROLE
from wf.entrypoints.web import pipeline as stage_view
from wf.entrypoints.web.viewmodels.base import ViewModel
from wf.entrypoints.web.viewmodels.board import when

SEND_BACK_STAGES = ("in_dev", "pr_open", "ready_for_qa")


@dataclass
class TicketViewModel(ViewModel):
    template = "ticket.html"
    active = "agents"
    ticket: dict = field(default_factory=dict)
    comments: list = field(default_factory=list)
    events: list = field(default_factory=list)
    noise_count: int = 0
    flag_reason: str = ""
    outbox_items: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    next_role: str | None = None
    notice: str = ""
    stages: list = field(default_factory=list)
    title: str = ""
    failures: list = field(default_factory=list)
    marker: tuple = ()
    flow: list = field(default_factory=list)

    @classmethod
    def load(cls, container, key: str, notice: str = "") -> "TicketViewModel | None":
        settings = container.settings()
        with container.session() as s:
            detail = s.queries.ticket_detail(key)
            if detail is None:
                return None
            runs = [dict(dataclasses.asdict(r), when=when(r.started_at, "%b %d %H:%M"))
                    for r in s.agent_runs.runs_for(key)]
            events = [dict(e, when=when(e["at"], "%b %d %H:%M")) for e in reversed(detail["events"])]
            noise = [e for e in events if e["actor"] == "sync" or (e["actor"] == "outbox" and e["kind"] == "stage")]
            events = [e for e in events if e not in noise]
            for c in detail["comments"]:
                c["preview"] = (c["body"] or "").strip().splitlines()[0][:160] if c.get("body") else ""
            observer = s.observer(settings)
            failures = observer.failures(key) + [
                {"step": f"{r['role']} agent on {r['stage']}", "job": "agent run", "url": "",
                 "reasons": ["timed out" if r["timed_out"] else f"exit {r['exit_code']}"],
                 "output": r["output_path"], "stderr": r["output_path"].replace("output.jsonl", "stderr.log")}
                for r in runs if r["exit_code"] not in (0, None) or r["timed_out"]]
            t = detail["ticket"]
            t["flag_list"] = json.loads(t["flags"] or "[]")
            t["label_list"] = json.loads(t["labels"] or "[]")
            pr_url = s.tickets.last_note(key, "pr")
            gh = s.pull_requests.status(pr_url) if pr_url else {}
            stage = t["stage"]
            return cls(
                ticket=t, comments=detail["comments"], events=events, noise_count=len(noise),
                flag_reason=next((e["detail"] for e in events if e["kind"] == "flag"), "") if t["flag_list"] else "",
                outbox_items=s.outbox.for_ticket(key), runs=runs,
                next_role=None if t["flag_list"] else STAGE_ROLE.get(stage or ""), notice=notice,
                stages=stage_view.progress(stage), title=key, failures=failures, marker=stage_view.stage_marker(stage),
                flow=stage_view.flow_states(
                    stage,
                    last_run_failed=bool(runs and (runs[0]["exit_code"] != 0 or runs[0]["timed_out"])),
                    perf_requested=s.tickets.perf_requested(key), pr_url=pr_url, pr_merged=gh.get("merged", False),
                    dev_deployed=observer.dev_deployed(key), prod_deployed=observer.prod_deployed(key),
                    dev_failed=bool((observer.latest(key, "dev:") or "").startswith("dev:failed")),
                    dev_running=observer.dev_running(key), pr_checks=gh.get("checks", "none"),
                    pr_closed=gh.get("ok") and gh.get("state") == "CLOSED", flagged=bool(t["flag_list"]),
                    prod_ready=cls._prod_ready(s, settings, key, stage)))

    @staticmethod
    def _prod_ready(session, settings, key: str, stage: str | None) -> bool:
        if stage not in ("regression", "performance"):
            return False
        try:
            return session.deploy(settings).prod_ready(key)
        except Exception:  # no repo linked, or git failed: the deploy button just stays hidden
            return False

    # --- what a person can do ----------------------------------------------------------------

    @staticmethod
    def send_back(container, key: str, to: str, reason: str) -> None:
        """Send a ticket back to an earlier stage with a reason. The pipeline restarts from there."""
        text = reason.strip()
        if to not in SEND_BACK_STAGES or not text:
            return
        try:
            with container.session() as s:
                s.tickets.add_comment(key, "user", f"Sent back to {to}: {text}", origin="user")
                s.tickets.move_stage(key, to, "user", f"sent back: {text[:120]}")
        except BoardError:
            pass

    @staticmethod
    def answer(container, key: str, answer: str) -> None:
        """The human's answer to a flagged question: it becomes a comment the next run reads, and the flags
        clear, so the pipeline can continue."""
        text = answer.strip()
        if not text:
            return
        with container.session() as s:
            s.tickets.add_comment(key, "user", text, origin="user")
            for flag in list(s.tickets.get(key).flags):
                s.tickets.clear_flag(key, flag, "user")

    @staticmethod
    def clear_flag(container, key: str, flag: str) -> None:
        try:
            with container.session() as s:
                s.tickets.clear_flag(key, flag, "user")
        except BoardError:
            pass
