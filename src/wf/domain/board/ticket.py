"""The Ticket aggregate: a ticket on the local board and the rules for changing it.

Every change goes through a method here. A method checks the rule, changes the ticket, and
records the TicketEvent that says what happened. The caller saves the ticket, which writes the
changed fields and the new events together.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wf.domain.board.errors import BoardError
from wf.domain.board.events import TicketEvent
from wf.domain.board.moves import FLAGS, may_move
from wf.domain.board.stages import STAGES

APPROVER_ROLES = ("pr_review", "perf_review", "qa", "performance")


def check_stage(stage: str) -> None:
    if stage not in STAGES:
        raise BoardError(f"unknown stage {stage!r}; choose one of {', '.join(STAGES)}")


def check_flag(flag: str) -> None:
    if flag not in FLAGS:
        raise BoardError(f"flag must be one of {FLAGS}")


def check_comment(body: str) -> None:
    if not body.strip():
        raise BoardError("a comment needs a body")


def check_approval(role: str, head: str) -> None:
    if role not in APPROVER_ROLES:
        raise BoardError(f"{role!r} cannot approve a ticket")
    if not head:
        raise BoardError("an approval needs the commit it covers")


def check_perf_request(actor: str) -> None:
    if actor != "dev":
        raise BoardError("only the dev role can request a performance check")


@dataclass
class Ticket:
    key: str
    project_key: str
    stage: str | None
    flags: list[str] = field(default_factory=list)
    removed: bool = False
    sort_order: int | None = None
    jira_status: str = ""
    summary: str = ""
    events: list[TicketEvent] = field(default_factory=list)  # recorded since it was loaded, not yet saved

    def _record(self, at: int, actor: str, kind: str, from_value=None, to_value=None, detail=None) -> TicketEvent:
        event = TicketEvent(self.key, at, actor, kind,
                            None if from_value is None else str(from_value),
                            None if to_value is None else str(to_value), detail)
        self.events.append(event)
        return event

    def move_to(self, stage: str, actor: str, reason: str, at: int) -> bool:
        """Move to an internal stage. False if it is already there."""
        check_stage(stage)
        if self.removed:
            raise BoardError(f"{self.key} is no longer in scope; it cannot be moved")
        if self.stage == stage:
            return False
        if not may_move(actor, self.stage, stage):
            raise BoardError(f"{actor!r} may not move {self.key} from {self.stage or '(no stage)'} to {stage!r}")
        previous, self.stage = self.stage, stage
        self._record(at, actor, "stage", previous, stage, reason)
        return True

    def flag(self, flag: str, actor: str, reason: str, at: int) -> bool:
        """Flag the ticket for a human. False if the flag is already set."""
        check_flag(flag)
        if flag in self.flags:
            return False
        self.flags.append(flag)
        self._record(at, actor, "flag", None, flag, reason)
        return True

    def clear_flag(self, flag: str, actor: str, at: int) -> bool:
        """Clear a flag. Only the user can. False if it was not set."""
        if actor != "user":
            raise BoardError("only the user can clear a flag")
        if flag not in self.flags:
            return False
        self.flags.remove(flag)
        self._record(at, actor, "unflag", flag, None, None)
        return True

    def comment(self, author: str, body: str, at: int) -> None:
        check_comment(body)
        self._record(at, author, "comment", None, None, body[:200])

    def approve(self, role: str, head: str, at: int) -> None:
        """A reviewer approves the ticket at a specific commit."""
        check_approval(role, head)
        self._record(at, role, "approve", None, head, head)

    def request_perf_check(self, actor: str, reason: str, at: int) -> None:
        check_perf_request(actor)
        self._record(at, actor, "perf_request", None, "requested", reason)

    def set_order(self, position: int, actor: str, at: int) -> None:
        """Where the ticket sits in the sprint's work order. Lower goes first."""
        previous, self.sort_order = self.sort_order, int(position)
        self._record(at, actor, "order", previous, int(position), "work order")

    def sent_to_jira(self, jira_status: str, at: int) -> None:
        """The Jira agent moved the real ticket; its Jira status is now `jira_status`."""
        self.jira_status = jira_status
        self._record(at, "jira-agent", "outbox", None, jira_status, "sent to Jira")

    def blocked_by(self, prerequisite: "Ticket", actor: str, reason: str, at: int) -> None:
        """Record that `prerequisite` must merge before this ticket starts."""
        self._record(at, actor, "link", None, prerequisite.key, reason)


def check_block(prerequisite: str, dependent: str) -> None:
    if prerequisite == dependent:
        raise BoardError("a ticket cannot block itself")
