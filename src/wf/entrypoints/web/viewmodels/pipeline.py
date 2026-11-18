"""The pipeline page: every ticket as a card in its stage's column, the activity log, and the actions on them."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import quote

from wf.application.harness.run_ticket import NotStarted
from wf.domain.board.errors import BoardError
from wf.domain.delivery.roles import STAGE_ROLE
from wf.domain.execution import run_history
from wf.domain.shared.ticket_key import TicketKey
from wf.entrypoints.web import activity
from wf.entrypoints.web import pipeline as stage_view
from wf.entrypoints.web.viewmodels.base import ViewModel


@dataclass
class CardViewModel(ViewModel):
    """One ticket's card. The card template binds to it as `t`."""
    template = "_card.html"
    key: str = ""
    stage: str = ""
    summary: str = ""
    flag_list: list = field(default_factory=list)
    idle: dict | None = None
    flag_reason: str = ""
    in_sprint: bool = False
    role: str | None = None
    can_merge: bool = False
    can_publish: bool = False
    can_deploy: bool = False
    last: dict | None = None
    state: dict = field(default_factory=dict)  # the activity log, so the buttons know when to wait

    def bindings(self) -> dict:
        return {"t": self, "stage": self.stage, "state": self.state, "vm": self}


@dataclass
class ColumnViewModel(ViewModel):
    """One stage's column: its label, owner, and cards."""
    template = "_col_meta.html"
    stage: str = ""
    label: str = ""
    owner: str = ""
    role: str | None = None
    tickets: list = field(default_factory=list)

    def bindings(self) -> dict:
        return {"col": self, "vm": self}


@dataclass
class ActivityViewModel(ViewModel):
    """What is running, the last result, and the log."""
    template = "_agents_state.html"
    state: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ActivityViewModel":
        return cls(state=activity.snapshot())


@dataclass
class PipelineBoardViewModel(ViewModel):
    """The columns alone: the page fragment the board refreshes."""
    template = "_board.html"
    columns: list = field(default_factory=list)
    queued: int = 0
    paused: bool = False
    paused_text: str = ""
    state: dict = field(default_factory=dict)
    repos: list = field(default_factory=list)
    access: bool = False
    test_cmd: str = ""
    model: str = ""
    runs_dir: str = ""

    @classmethod
    def load(cls, container) -> "PipelineBoardViewModel":
        settings = container.settings()
        state = activity.snapshot()
        with container.session() as s:
            pipeline, deploy = s.pipeline(settings), s.deploy(settings)
            paused_at = s.pause.paused_since()
            by_stage: dict[str, list[CardViewModel]] = {}
            for r in sorted(s.queries.pipeline_rows(), key=lambda r: r["key"]):
                last_code = run_history.last_exit_code(s.agent_runs.runs_for(r["key"]))
                flags = r["flags"]
                reason = s.tickets.last_note(r["key"], "flag")
                by_stage.setdefault(r["stage"], []).append(CardViewModel(
                    key=r["key"], stage=r["stage"], summary=r["summary"], flag_list=flags,
                    idle=pipeline.idle_reason(r["key"]), flag_reason=(reason or "") if flags else "",
                    in_sprint=r["sprint_state"] == "active", role=None if flags else STAGE_ROLE.get(r["stage"]),
                    can_publish=r["stage"] == "deployed",
                    can_deploy=r["stage"] in ("regression", "performance") and deploy.prod_ready(r["key"]),
                    last={"exit_code": last_code} if last_code is not None else None, state=state))
            queued, paused = s.outbox.pending_count(), s.pause.is_paused()
        columns = [ColumnViewModel(stage=st["name"], label=st["label"], owner=st["owner"], role=st["role"],
                                   tickets=by_stage.get(st["name"], [])) for st in stage_view.ordered_stages()]
        return cls(columns=columns, queued=queued, paused=paused,
                   paused_text=time.strftime("%H:%M", time.localtime(paused_at)) if paused_at else "", state=state,
                   repos=settings.repos, access=settings.claude_code_access, test_cmd=settings.test_cmd,
                   model=settings.model, runs_dir=str(settings.runs_dir))


@dataclass
class PipelineViewModel(PipelineBoardViewModel):
    """The whole pipeline page."""
    template = "agents.html"
    active = "agents"


@dataclass
class TicketLiveViewModel(ViewModel):
    """A ticket page's live status: whether it is running, and why it is not moving."""
    template = "_ticket_live.html"
    ticket: dict = field(default_factory=dict)
    idle: dict | None = None
    running: str | None = None
    other: str | None = None

    @classmethod
    def load(cls, container, key: str) -> "TicketLiveViewModel":
        settings = container.settings()
        with container.session() as s:
            idle = s.pipeline(settings).idle_reason(key)
        running = activity.snapshot()["running"]
        mine = running if running and running.startswith(key + " ") else None
        return cls(ticket={"key": key}, idle=idle, running=mine, other=running if running and not mine else None)


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


# --- what a person can do on the pipeline page ----------------------------------------------------

class PipelineCommands:
    """The buttons: each one calls a use case, tells the activity log, and says where to go next."""

    def __init__(self, container):
        self.c = container
        self.log = activity.log

    def pause(self) -> None:
        with self.c.session() as s:
            changed = s.pause.pause("paused by a person", int(self.c.clock()))
        self.log("work paused: no new run or stage move starts; a run already in progress finishes"
                 if changed else "work was already paused")

    def resume(self) -> None:
        with self.c.session() as s:
            changed = s.pause.resume(int(self.c.clock()))
        self.log("work resumed" if changed else "work was not paused")

    def run_pass(self) -> None:
        settings = self.c.settings()
        with self.c.session() as s:
            paused = s.pause.is_paused()
        if paused:
            self.log("work is paused: resume it before starting a pass")
        elif not settings.repos:
            self.log("no repo is linked: add one on the Links page first")
        elif not activity.start("one pass", self.c.background_work(settings).run_pass):
            self.log("a pass is already running")

    def run_one(self, key: str) -> str:
        """Start the agent for a ticket's stage by hand. Returns the notice to show on the ticket page."""
        settings = self.c.settings()
        with self.c.session() as s:
            try:
                plan = s.run_ticket(settings).prepare(key)
            except NotStarted as e:
                self.log(f"{key}: {e}")
                return str(e)

        def work():
            with self.c.session() as s:
                code = s.run_ticket(settings).execute(plan)
            self.log(f"{key}: {plan.role} finished (exit {code})")

        if not activity.start(f"{key} {plan.role}", work):
            notice = "Not started: another action is already running. Try again when it finishes."
        else:
            notice = f"Started the {plan.role} agent on {key}. The result appears below and in the Activity log."
        self.log(f"{key}: {notice}")
        return notice

    @staticmethod
    def ticket_url(key: str, notice: str) -> str:
        return f"/board/ticket/{key}?notice={quote(notice)}"

    def deploy(self, key: str, confirm: str) -> None:
        """The one manual step: a person deploys a ticket that has passed regression."""
        if confirm != "yes":
            return self.log(f"{key}: deploy to prod needs the confirm box ticked")
        settings = self.c.settings()
        try:
            with self.c.session() as s:
                s.deploy(settings).deploy_to_prod(key, settings.repo_slug())
            self.log(f"{key} deployed to prod (by you)")
        except BoardError as e:
            self.log(f"{key}: deploy refused: {e}")

    def merge(self, key: str) -> None:
        settings = self.c.settings()

        def work():
            with self.c.session() as s:
                head = s.merge(settings).merge(key, settings.test_cmd)
            self.log(f"{key} merged into main at {head[:8]}")

        if not activity.start(f"merge {key}", work):
            self.log("another action is already running")

    def publish(self, key: str, confirm: str) -> None:
        if confirm != "yes":
            return self.log(f"{key}: publish needs the confirm box ticked")
        settings = self.c.settings()

        def work():
            with self.c.session() as s:
                url = s.publish(settings).open_pull_request(key, confirm=True)
            self.log(f"{key} pull request: {url}")

        if not activity.start(f"publish {key}", work):
            self.log("another action is already running")

    def jira_sync(self) -> None:
        if not activity.start("jira sync", self.c.background_work(self.c.settings()).jira_sync):
            self.log("another action is already running")

    def jira_send(self, confirm: str) -> None:
        if confirm != "yes":
            return self.log("sending to Jira needs the confirm box ticked")
        settings = self.c.settings()

        def work():
            with self.c.session() as s:
                s.jira(settings).send_queued()

        if not activity.start("send to Jira", work):
            self.log("another action is already running")

    def move_tickets_to_sprint(self, keys: list[str]) -> None:
        """Move the tickets the browser sent into the active sprint, in one Jira write."""
        keys = [k for k in keys if TicketKey.is_valid(k)]
        settings = self.c.settings()
        with self.c.session() as s:
            sprint = s.queries.active_sprint()
        if sprint is None or not keys:
            return self.log("hopper: nothing to move, or no active sprint")

        def work():
            with self.c.session() as s:
                s.jira(settings).move_tickets_to_sprint(keys, sprint)

        if not activity.start(f"move {len(keys)} ticket(s) to sprint", work):
            self.log("another action is already running")

    def move_to_sprint(self, key: str) -> None:
        """One of the user's tickets joins the active sprint. Jira is written, then read back."""
        settings = self.c.settings()
        with self.c.session() as s:
            sprint = s.queries.active_sprint()
        if sprint is None:
            return self.log(f"{key}: no active sprint to move into")

        def work():
            with self.c.session() as s:
                s.jira(settings).move_to_sprint(key, sprint)

        if not activity.start(f"move {key} to sprint", work):
            self.log("another action is already running")
