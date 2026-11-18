"""The pipeline's decisions, as pure functions of facts.

An application service gathers the facts through ports, calls one of these, and acts on the answer.
Nothing here reads a database, a repository, or the clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wf.domain.shared.sprint import SprintState
from wf.domain.shared.stage import Stage
from wf.domain.delivery.roles import (
    MAX_RUNS_PER_STAGE, REFINEMENT_STAGES, STAGE_ROLE, refinement_limit_reached, run_limit_reached,
)

# Stages whose next step the harness decides without an agent.
ADVANCE_STAGES = ("pr_open", "pr_in_review", "merged", "regression", "performance")


@dataclass(frozen=True)
class Move:
    to_stage: str
    reason: str


SPRINT_BACKLOG_MOVE = Move("in_refinement", "in the active sprint: refine it")


def paused_move(blocker: str) -> Move:
    """A prerequisite sent back to dev pauses the work that depends on it."""
    return Move("ready_for_dev", f"paused: prerequisite {blocker} was sent back to dev")


@dataclass(frozen=True)
class AdvanceFacts:
    """What the harness needs to know to move one ticket along. Sets are roles that approved."""
    stage: str
    flagged: bool = False  # a person has to clear a flag before the harness moves the ticket
    head: str | None = None  # the branch's current commit, None if the branch does not exist
    dev_deployed: bool = False
    approved_at_head: frozenset[str] = frozenset()
    approved_since_regression: frozenset[str] = frozenset()
    approved_since_performance: frozenset[str] = frozenset()
    perf_requested: bool = False


def advance_move(f: AdvanceFacts) -> Move | None:
    """The move the harness makes for a ticket in one of ADVANCE_STAGES, or None.

    - a merged ticket goes to regression once it is deployed to dev
    - a QA approval of the current commit after regression began passes it (to performance if the dev
      asked for a check, else to deployed)
    - a performance approval of the current commit after performance began passes it
    - a PR review approval of the current commit sends the ticket on to the performance review
    A flagged ticket is held, whatever it has been approved for: a later flag means a person has to look.
    """
    if f.flagged:
        return None
    if f.stage == "merged":
        return Move("regression", "deployed to dev; regression on dev") if f.dev_deployed else None
    if f.head is None:
        return None
    short = f.head[:8]
    if f.stage == "regression":
        if "qa" not in f.approved_since_regression:
            return None
        if f.perf_requested:
            return Move("performance", "regression passed; performance requested")
        return Move("deployed", f"regression passed at {short}; deployed to dev")
    if f.stage == "performance":
        if "performance" in f.approved_since_performance:
            return Move("deployed", f"performance passed at {short}")
        return None
    if f.stage in ("pr_open", "pr_in_review") and "pr_review" in f.approved_at_head:
        return Move("ready_for_perf_review", f"PR review approved at {short}")
    return None


def prod_ready(head: str | None, approved_since_regression: frozenset[str] | set[str], perf_requested: bool,
               approved_since_performance: frozenset[str] | set[str]) -> bool:
    """Regression passed on the current commit (and performance, if the dev asked for it).

    Only then does the Deploy to prod button appear. Approvals count only after the ticket entered the stage
    they belong to.
    """
    if head is None or "qa" not in approved_since_regression:
        return False
    if perf_requested:
        return "performance" in approved_since_performance
    return True


# --- one pass of the loop ----------------------------------------------------------------------

@dataclass(frozen=True)
class PassTicket:
    key: str
    stage: str
    flags: tuple[str, ...]
    sort_order: int | None
    sprint_state: str | None


@dataclass(frozen=True)
class TicketFacts:
    """What a pass needs to know about a ticket that could run. Computed only for candidates."""
    refinement_role: str | None = None  # whose turn it is, when the ticket is in refinement
    finished_and_unchanged: bool = False  # the last run here succeeded and nothing has happened since
    product_rounds: int = 0
    failed_in_a_row: int = 0
    unmet_blockers: tuple[str, ...] = ()


@dataclass
class PlannedAction:
    key: str
    stage: str
    role: str
    skipped: str | None = None
    flag_needs_human: bool = False  # the pass should flag the ticket for a person
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"key": self.key, "stage": self.stage, "role": self.role, "skipped": self.skipped, **self.extra}


def work_order(tickets: list[PassTicket]) -> list[PassTicket]:
    """A ticket already in development first, then by the sprint's order (unordered last), then by key."""
    return sorted(tickets, key=lambda t: (t.stage != "in_dev", t.sort_order is None, t.sort_order or 0, t.key))


def is_candidate(t: PassTicket) -> bool:
    """A ticket an agent may work on: it has a role, is in an active sprint, and is not flagged."""
    role = STAGE_ROLE.get(t.stage)
    if t.stage in REFINEMENT_STAGES:
        role = "product"  # the actual turn is a fact: see TicketFacts.refinement_role
    return role is not None and t.sprint_state == SprintState.ACTIVE and not t.flags


def plan_pass(tickets: list[PassTicket], facts: dict[str, TicketFacts]) -> list[PlannedAction]:
    """Which ticket gets which role this pass, and which are held back and why.

    One dev at a time: a ready ticket is picked up only when no ticket is in development, and only when
    its prerequisites are on dev. A ticket in refinement gets at most MAX_REFINEMENT_ROUNDS product
    rounds before a person is asked.
    """
    dev_busy = next((t.key for t in tickets if t.stage == Stage.IN_DEV and t.sprint_state == SprintState.ACTIVE), None)
    actions: list[PlannedAction] = []
    for t in work_order(tickets):
        if not is_candidate(t):
            continue
        f = facts[t.key]
        role = f.refinement_role if t.stage in REFINEMENT_STAGES else STAGE_ROLE.get(t.stage)
        if t.stage not in REFINEMENT_STAGES and f.finished_and_unchanged:
            continue  # the last run here succeeded and nothing has changed since: wait for a change
        action = PlannedAction(t.key, t.stage, role)
        if t.stage in REFINEMENT_STAGES:
            if refinement_limit_reached(f.product_rounds):
                action.skipped = f"{f.product_rounds} refinement rounds without agreement; needs a human"
                action.flag_needs_human = True
                actions.append(action)
                continue
        elif run_limit_reached(f.failed_in_a_row):
            action.skipped = "run limit reached for this stage; needs a human"
            actions.append(action)
            continue
        if t.stage == "ready_for_dev":
            if dev_busy is not None:
                action.skipped = f"dev is busy with {dev_busy}"
                actions.append(action)
                continue
            if f.unmet_blockers:
                action.skipped = f"waiting for {', '.join(f.unmet_blockers)} to merge to dev"
                actions.append(action)
                continue
        if t.stage == Stage.IN_DEV and dev_busy != t.key:
            action.skipped = f"dev is busy with {dev_busy}"
            actions.append(action)
            continue
        if t.stage == "ready_for_dev":
            dev_busy = t.key  # the dev holds this ticket now; no other ready ticket starts this pass
        actions.append(action)
    return actions


# --- why a ticket is not moving ----------------------------------------------------------------

@dataclass(frozen=True)
class IdleFacts:
    stage: str | None
    flags: tuple[str, ...]
    sprint_state: str | None
    paused: bool
    role: str | None  # the role that owns the stage now (the refinement turn when in refinement)
    product_rounds: int = 0
    failed_in_a_row: int = 0
    unmet_blockers: tuple[str, ...] = ()
    finished_and_unchanged: bool = False


def idle_reason(f: IdleFacts) -> dict | None:
    """Why a ticket is not moving, or None when the next pass will pick it up.

    Returns {"why": text, "can_run": bool}. can_run means a person can start the stage's agent now with
    the Run again button. It is False when something else has to change first.
    """
    if f.stage is None:
        return None
    if f.paused:
        return {"why": "Work is paused. It resumes when you press Resume work.", "can_run": False}
    if f.flags:
        return {"why": f"Flagged: {', '.join(f.flags)}. Clear the flag first.", "can_run": False}
    if f.role is None:
        return None  # nothing is expected to happen here (backlog, done): no notice
    if f.sprint_state != SprintState.ACTIVE:
        return {"why": "Not in an active sprint, so no agent picks it up.", "can_run": False}
    if f.stage in REFINEMENT_STAGES:
        if refinement_limit_reached(f.product_rounds):
            return {"why": f"{f.product_rounds} refinement rounds without agreement. A person has to decide.",
                    "can_run": True}
    elif run_limit_reached(f.failed_in_a_row):
        return {"why": f"{MAX_RUNS_PER_STAGE} runs in a row failed here. A person has to look at the last run.",
                "can_run": True}
    elif f.stage == "ready_for_dev":
        if f.unmet_blockers:
            return {"why": f"Waiting for {', '.join(f.unmet_blockers)} to reach dev first.", "can_run": False}
    elif f.finished_and_unchanged:
        return {"why": f"The {f.role} agent finished its last run here and nothing has changed since. "
                       "It won't run again until something changes or you run it.", "can_run": True}
    return None
