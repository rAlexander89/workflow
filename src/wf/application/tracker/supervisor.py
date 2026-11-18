"""The supervisor: routes each ticket status change to the agent that owns its stage.

A Jira status is mapped to an internal stage, and the stage has an owner. Both steps are fixed rules
(`wf.domain.delivery.routing`), not a model call. A status with no stage is "unknown" and nothing is
dispatched. Every decision is written to the lifecycle record. A kill switch halts all dispatch, and a
loop guard escalates a ticket that keeps re-entering the same status.
"""

from __future__ import annotations

from typing import Callable, Mapping

from wf.application.ports.persistence import LifecycleLog
from wf.domain.delivery.routing import MAX_DISPATCHES_PER_STATUS, Decision, route
from wf.domain.tracker.changelog import StatusChange

Dispatch = Callable[[str, str, str], bool]  # (owner, issue_key, status) -> dispatched?


def no_agents_wired(owner: str, issue_key: str, status: str) -> bool:
    """Placeholder until the agents exist: nothing runs, nothing is dispatched."""
    return False


class Supervisor:
    def __init__(
        self,
        log: LifecycleLog,
        status_to_stage: Callable[[], Mapping[str, str]],
        dispatch: Dispatch = no_agents_wired,
        max_dispatches_per_status: int = MAX_DISPATCHES_PER_STATUS,
    ):
        self.store = log
        self._dispatch = dispatch
        self._max = max_dispatches_per_status
        self._status_to_stage = status_to_stage

    def handle(self, change: StatusChange, labels: set[str] | frozenset[str] = frozenset()) -> Decision:
        if self.store.killed():
            decision = Decision("halt", None, "kill switch is on: nothing is dispatched")
            self.store.record(change, decision, dispatched=False)
            return decision

        decision = route(change.to_status, labels, self._status_to_stage())

        if decision.action in ("dispatch", "merge"):
            if self.store.dispatch_count(change.issue_key, change.to_status) >= self._max:
                decision = Decision(
                    "escalate",
                    None,
                    f"{change.to_status!r} reached {self._max} times: needs a human",
                )

        dispatched = False
        if decision.action in ("dispatch", "merge"):
            dispatched = bool(self._dispatch(decision.owner, change.issue_key, change.to_status))
        self.store.record(change, decision, dispatched=dispatched)
        return decision
