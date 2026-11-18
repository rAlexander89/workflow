"""The Ticket aggregate: a ticket on the local board and the rules for changing it.

Every change goes through a method here. A method checks the rule, changes the ticket, and
records the TicketEvent that says what happened. The caller saves the ticket, which writes the
changed fields and the new events together.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wf.domain.board.errors import BoardError
from wf.domain.board.events import (
    ApprovalRecorded, DomainEvent, FlagCleared, PerfCheckRequested, SentToJira, TicketBlocked, TicketCommented,
    TicketFlagged, TicketMoved, TicketReordered,
)
from wf.domain.board.moves import FLAGS, may_move
from wf.domain.board.stages import STAGES
from wf.domain.shared.roles import APPROVER_ROLES, Actor, Role

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
    if actor != Role.DEV:
        raise BoardError("only the dev role can request a performance check")


@dataclass
class Ticket:
    """A ticket on the local board. The aggregate root: every change goes through a method here, which checks
    the rule, changes the ticket, and records the domain event that says what happened."""
    key: str
    project_key: str
    stage: str | None
    flags: list[str] = field(default_factory=list)
    removed: bool = False
    sort_order: int | None = None
    jira_status: str = ""
    summary: str = ""
    _pending: list[DomainEvent] = field(default_factory=list, repr=False, compare=False)

    @property
    def events(self) -> list[DomainEvent]:
        """The events recorded since the ticket was loaded and not yet saved."""
        return self._pending

    def pull_events(self) -> list[DomainEvent]:
        """Hand over the recorded events, and forget them. The repository saves them; the bus publishes them."""
        out, self._pending = self._pending, []
        return out

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
        self._pending.append(TicketMoved(self.key, at, actor, self.project_key, previous, stage, reason))
        return True

    def flag(self, flag: str, actor: str, reason: str, at: int) -> bool:
        """Flag the ticket for a human. False if the flag is already set."""
        check_flag(flag)
        if flag in self.flags:
            return False
        self.flags.append(flag)
        self._pending.append(TicketFlagged(self.key, at, actor, flag, reason))
        return True

    def clear_flag(self, flag: str, actor: str, at: int) -> bool:
        """Clear a flag. Only the user can. False if it was not set."""
        if actor != Actor.USER:
            raise BoardError("only the user can clear a flag")
        if flag not in self.flags:
            return False
        self.flags.remove(flag)
        self._pending.append(FlagCleared(self.key, at, actor, flag))
        return True

    def comment(self, author: str, body: str, at: int) -> None:
        check_comment(body)
        self._pending.append(TicketCommented(self.key, at, author, body[:200]))

    def approve(self, role: str, head: str, at: int) -> None:
        """A reviewer approves the ticket at a specific commit."""
        check_approval(role, head)
        self._pending.append(ApprovalRecorded(self.key, at, role, head))

    def request_perf_check(self, actor: str, reason: str, at: int) -> None:
        check_perf_request(actor)
        self._pending.append(PerfCheckRequested(self.key, at, actor, reason))

    def set_order(self, position: int, actor: str, at: int) -> None:
        """Where the ticket sits in the sprint's work order. Lower goes first."""
        previous, self.sort_order = self.sort_order, int(position)
        self._pending.append(TicketReordered(self.key, at, actor, previous, int(position)))

    def sent_to_jira(self, jira_status: str, at: int) -> None:
        """The Jira agent moved the real ticket; its Jira status is now `jira_status`."""
        self.jira_status = jira_status
        self._pending.append(SentToJira(self.key, at, Actor.JIRA_AGENT, jira_status))

    def blocked_by(self, prerequisite: "Ticket", actor: str, reason: str, at: int) -> None:
        """Record that `prerequisite` must merge before this ticket starts."""
        self._pending.append(TicketBlocked(self.key, at, actor, prerequisite.key, reason))


def check_block(prerequisite: str, dependent: str) -> None:
    if prerequisite == dependent:
        raise BoardError("a ticket cannot block itself")
