"""The pipeline: move tickets along, plan a pass of the loop, and explain why a ticket is not moving.

Each method gathers facts through the injected ports, asks a pure function in
`wf.domain.delivery.pipeline` what to do, and acts on the answer through the ticket commands.
"""

from __future__ import annotations

from wf.application.board.tickets import TicketCommands
from wf.application.ports.board import BoardQueries
from wf.application.ports.delivery import DeployObserver, Workspace
from wf.application.ports.persistence import AgentRunRepository, PauseState
from wf.domain.delivery import pipeline as rules
from wf.domain.execution import run_history
from wf.domain.delivery.roles import PAUSABLE, REFINEMENT_STAGES, STAGE_ROLE, next_refinement_role, unmet_blockers

HARNESS = "harness"


class PipelineService:
    def __init__(self, *, commands: TicketCommands, queries: BoardQueries, pause: PauseState,
                 runs: AgentRunRepository, workspace: Workspace, deploys: DeployObserver):
        self._commands = commands
        self._queries = queries
        self._pause = pause
        self._runs = runs
        self._workspace = workspace
        self._deploys = deploys

    # --- advancing --------------------------------------------------------------------------

    def advance(self) -> list[str]:
        """Move tickets along the steps that need no agent judgement. Returns the keys moved.

        Deploying to prod is a person's step; nothing here moves a ticket past regression.
        While work is paused, no ticket moves.
        """
        if self._pause.is_paused():
            return []
        moved: list[str] = []
        for key in self._queries.sprint_backlog_keys():
            self._move(key, rules.SPRINT_BACKLOG_MOVE)
            moved.append(key)
        for row in self._queries.kicked_back_dependents(PAUSABLE):
            self._move(row["key"], rules.paused_move(row["blocker"]))
            moved.append(row["key"])
        for row in self._queries.keys_in_stages(rules.ADVANCE_STAGES):
            ticket = self._commands.get(row["key"])
            if ticket is None or ticket.flags:
                continue  # held until a person clears the flag
            move = rules.advance_move(self._advance_facts(row["key"], row["stage"]))
            if move is not None:
                self._move(row["key"], move)
                moved.append(row["key"])
        return moved

    def _move(self, key: str, move: rules.Move) -> None:
        self._commands.move_stage(key, move.to_stage, HARNESS, move.reason)

    def _advance_facts(self, key: str, stage: str) -> rules.AdvanceFacts:
        if stage == "merged":
            return rules.AdvanceFacts(stage, dev_deployed=self._deploys.dev_deployed(key))
        head = self._workspace.branch_head(key)
        if head is None:
            return rules.AdvanceFacts(stage)
        if stage == "regression":
            return rules.AdvanceFacts(
                stage, head=head,
                approved_since_regression=frozenset(self._commands.approvals_since_stage(key, head, "regression")),
                perf_requested=self._commands.perf_requested(key))
        if stage == "performance":
            return rules.AdvanceFacts(
                stage, head=head,
                approved_since_performance=frozenset(self._commands.approvals_since_stage(key, head, "performance")))
        return rules.AdvanceFacts(stage, head=head, approved_at_head=frozenset(self._commands.approvals_at(key, head)))

    # --- one pass ---------------------------------------------------------------------------

    def plan_pass(self) -> list[rules.PlannedAction]:
        """Which ticket gets which role this pass, and which are held back and why. Empty while paused."""
        if self._pause.is_paused():
            return []
        rows = self._queries.pipeline_rows()
        stage_of = {r["key"]: r["stage"] for r in rows}
        tickets = [rules.PassTicket(r["key"], r["stage"], tuple(r["flags"]), r["sort_order"], r["sprint_state"])
                   for r in rows]
        facts = {t.key: self._ticket_facts(t.key, t.stage, stage_of) for t in tickets if rules.is_candidate(t)}
        return rules.plan_pass(tickets, facts)

    def _ticket_facts(self, key: str, stage: str, stage_of: dict[str, str]) -> rules.TicketFacts:
        in_refinement = stage in REFINEMENT_STAGES
        return rules.TicketFacts(
            refinement_role=self.refinement_role(key) if in_refinement else None,
            finished_and_unchanged=False if in_refinement else self._finished_and_unchanged(key, stage),
            product_rounds=run_history.product_rounds(self._runs.runs_for(key)) if in_refinement else 0,
            failed_in_a_row=0 if in_refinement else run_history.failed_in_a_row(self._runs.runs_for(key), stage),
            unmet_blockers=tuple(unmet_blockers(self._commands.blockers(key), stage_of))
            if stage == "ready_for_dev" else (),
        )

    def refinement_role(self, key: str) -> str:
        """Whose turn it is in refinement: product goes first, then staff, and so on."""
        return next_refinement_role(run_history.last_role_at(self._runs.runs_for(key), "in_refinement"))

    def _finished_and_unchanged(self, key: str, stage: str) -> bool:
        """True when the last run in this stage succeeded and nothing has happened to the ticket since.

        An event by anyone other than that run's own role counts as a change, so a review that sends the
        ticket back to this stage makes the stage run again.
        """
        last = run_history.last_run(self._runs.runs_for(key), stage)
        if last is None or last.failed:
            return False
        return not self._commands.changed_since(key, last.started_at, last.role)

    # --- why a ticket is not moving ---------------------------------------------------------

    def idle_reason(self, key: str) -> dict | None:
        """Why a ticket is not moving, or None when the next pass will pick it up."""
        row = self._queries.pipeline_row(key)
        if row is None or row["stage"] is None:
            return None
        stage = row["stage"]
        in_refinement = stage in REFINEMENT_STAGES
        role = self.refinement_role(key) if in_refinement else STAGE_ROLE.get(stage)
        return rules.idle_reason(rules.IdleFacts(
            stage=stage, flags=tuple(row["flags"]), sprint_state=row["sprint_state"],
            paused=self._pause.is_paused(), role=role,
            product_rounds=run_history.product_rounds(self._runs.runs_for(key)) if in_refinement else 0,
            failed_in_a_row=0 if in_refinement else run_history.failed_in_a_row(self._runs.runs_for(key), stage),
            unmet_blockers=tuple(unmet_blockers(self._commands.blockers(key), self._queries.stage_by_key()))
            if stage == "ready_for_dev" else (),
            finished_and_unchanged=False if in_refinement else self._finished_and_unchanged(key, stage),
        ))
