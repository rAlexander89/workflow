"""In-memory implementations of the persistence ports. The contract tests keep them honest."""
from __future__ import annotations

from dataclasses import asdict
from typing import Callable, Iterable

from wf.domain.delivery.routing import Decision
from wf.domain.execution.run import Run
from wf.domain.execution.usage import AgentAudit, UsageRecord
from wf.domain.tracker.changelog import StatusChange


class MemoryRunRegistry:
    def __init__(self, clock: Callable[[], float] = lambda: 0.0):
        self._clock = clock
        self._runs: dict[str, Run] = {}

    def create(self, run_id, request, status="running"):
        run = Run(run_id, request, status, int(self._clock()))
        self._runs[run_id] = run
        return run

    def set_status(self, run_id, status):
        if run_id in self._runs:
            self._runs[run_id] = Run(**{**asdict(self._runs[run_id]), "status": status})

    def get(self, run_id):
        return self._runs.get(run_id)

    def list(self):
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)


class MemoryUsageLog:
    def __init__(self):
        self._rows: list[UsageRecord] = []

    def record(self, rec: UsageRecord) -> None:
        if min(rec.input_tokens, rec.output_tokens, rec.cache_read_tokens, rec.cache_write_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        self._rows.append(rec)

    def audit(self) -> list[AgentAudit]:
        out = []
        for agent in sorted({r.agent for r in self._rows}):
            rows = [r for r in self._rows if r.agent == agent]
            tin = sum(r.input_tokens for r in rows)
            tout = sum(r.output_tokens for r in rows)
            cache = sum(r.cache_read_tokens for r in rows)
            all_in = sum(r.cache_read_tokens + r.cache_write_tokens + r.input_tokens for r in rows)
            out.append(AgentAudit(agent, len(rows), tin / len(rows), tout / len(rows), tin, tout,
                                  cache / all_in if all_in else 0.0))
        return out

    def largest_calls(self, limit: int = 5) -> list[dict]:
        ranked = sorted(self._rows, key=lambda r: r.input_tokens + r.output_tokens, reverse=True)[:limit]
        return [{"agent": r.agent, "ticket": r.ticket, "model": r.model, "input_tokens": r.input_tokens,
                 "output_tokens": r.output_tokens} for r in ranked]


class MemoryChangeLog:
    def __init__(self):
        self._seen: set[tuple[str, str]] = set()
        self._webhook: float | None = None

    def record_new(self, changes: Iterable[StatusChange]) -> list[StatusChange]:
        new = []
        for c in changes:
            if (c.issue_key, c.history_id) not in self._seen:
                self._seen.add((c.issue_key, c.history_id))
                new.append(c)
        return new

    def last_webhook_at(self):
        return self._webhook

    def mark_webhook(self, when=None):
        self._webhook = 0.0 if when is None else when


class MemoryLifecycleLog:
    def __init__(self):
        self._rows: list[dict] = []
        self._kill = False

    def record(self, change: StatusChange, decision: Decision, dispatched: bool) -> None:
        self._rows.append({"issue_key": change.issue_key, "from_status": change.from_status,
                           "to_status": change.to_status, "action": decision.action, "owner": decision.owner,
                           "reason": decision.reason, "dispatched": int(dispatched)})

    def dispatch_count(self, issue_key, status):
        return sum(1 for r in self._rows if r["issue_key"] == issue_key and r["to_status"] == status and r["dispatched"])

    def history(self, issue_key):
        return [dict(r) for r in self._rows if r["issue_key"] == issue_key]

    def set_kill(self, on):
        self._kill = on

    def killed(self):
        return self._kill


class MemoryPauseState:
    def __init__(self):
        self._since: int | None = None

    def is_paused(self):
        return self._since is not None

    def paused_since(self):
        return self._since

    def pause(self, reason, now):
        if self.is_paused():
            return False
        self._since = now
        return True

    def resume(self, now):
        if not self.is_paused():
            return False
        self._since = None
        return True


class MemoryAgentRuns:
    def __init__(self):
        self._rows: list[dict] = []

    def record(self, key, stage, role, started_at, exit_code, timed_out, output_path):
        self._rows.append({"id": len(self._rows) + 1, "key": key, "stage": stage, "role": role,
                           "started_at": started_at, "exit_code": exit_code, "timed_out": int(timed_out),
                           "output_path": str(output_path)})

    def _at(self, key, stage):
        return [r for r in reversed(self._rows) if r["key"] == key and r["stage"] == stage]

    def last_role_at(self, key, stage):
        rows = self._at(key, stage)
        return rows[0]["role"] if rows else None

    def last_run(self, key, stage):
        rows = self._at(key, stage)
        if not rows:
            return None
        r = rows[0]
        return {k: r[k] for k in ("role", "exit_code", "timed_out", "started_at")}

    def failed_in_a_row(self, key, stage):
        streak = 0
        for r in self._at(key, stage):
            if r["timed_out"] or r["exit_code"] != 0:
                streak += 1
            else:
                break
        return streak

    def product_rounds(self, key):
        return sum(1 for r in self._rows if r["key"] == key and r["role"] == "product")

    def runs_for(self, key):
        return [dict(r) for r in reversed(self._rows) if r["key"] == key]

    def last_exit_code(self, key, role=None):
        rows = [r for r in reversed(self._rows) if r["key"] == key and (role is None or r["role"] == role)]
        return rows[0]["exit_code"] if rows else None


import copy  # noqa: E402

from wf.domain.board.events import TicketEvent  # noqa: E402
from wf.domain.board.ticket import Ticket  # noqa: E402


class MemoryBoardState:
    def __init__(self):
        self.tickets: dict[str, Ticket] = {}
        self.history: dict[str, list[TicketEvent]] = {}
        self.comments: list[dict] = []
        self.links: list[list] = []  # [prerequisite, dependent, synced]
        self.mappings: dict[tuple[str, str], str] = {}
        self.outbox: list[dict] = []

    def add_ticket(self, key, stage, project_key="P", **kw):
        self.tickets[key] = Ticket(key, project_key, stage, **kw)
        self.history.setdefault(key, [])


class MemoryTicketRepository:
    def __init__(self, state: MemoryBoardState):
        self._s = state

    def get(self, key):
        t = self._s.tickets.get(key)
        return None if t is None else Ticket(t.key, t.project_key, t.stage, list(t.flags), t.removed,
                                             t.sort_order, t.jira_status, t.summary)

    def save(self, ticket):
        self._s.tickets[ticket.key] = Ticket(ticket.key, ticket.project_key, ticket.stage, list(ticket.flags),
                                             ticket.removed, ticket.sort_order, ticket.jira_status, ticket.summary)
        self._s.history.setdefault(ticket.key, []).extend(ticket.events)
        ticket.events.clear()

    def history(self, key):
        return list(self._s.history.get(key, []))

    def last_event_detail(self, key, kind):
        rows = [e for e in reversed(self._s.history.get(key, [])) if e.kind == kind]
        return rows[0].detail if rows else None

    def event_details(self, key, kind):
        return [e.detail for e in reversed(self._s.history.get(key, [])) if e.kind == kind]

    def append_event(self, event):
        self._s.history.setdefault(event.ticket_key, []).append(event)

    def add_comment(self, key, origin, author, body, at):
        self._s.comments.append({"key": key, "origin": origin, "author": author, "body": body, "at": at})
        return len(self._s.comments)

    def link_blocks(self, prerequisite, dependent):
        if not any(l[0] == prerequisite and l[1] == dependent for l in self._s.links):
            self._s.links.append([prerequisite, dependent, 0])

    def blockers(self, key):
        return sorted(l[0] for l in self._s.links if l[1] == key)

    def pending_links(self):
        return sorted((l[0], l[1]) for l in self._s.links if l[2] == 0)

    def mark_linked(self, pairs):
        for l in self._s.links:
            if (l[0], l[1]) in pairs:
                l[2] = 1

    def set_jira_status(self, key, status):
        self._s.tickets[key].jira_status = status


class MemoryStageMappings:
    def __init__(self, state: MemoryBoardState):
        self._s = state

    def status_for(self, project_key, stage):
        return self._s.mappings.get((project_key, stage))


class MemoryOutboxQueue:
    def __init__(self, state: MemoryBoardState):
        self._s = state

    def enqueue_transition(self, ticket_key, stage, actor, now):
        for row in self._s.outbox:
            if row["ticket_key"] == ticket_key and row["status"] == "pending":
                row["status"] = "superseded"
        row = {"id": len(self._s.outbox) + 1, "ticket_key": ticket_key, "target_stage": stage, "actor": actor,
               "status": "pending", "created_at": now}
        self._s.outbox.append(row)
        return row["id"]

    def pending(self):
        out = []
        for r in self._s.outbox:
            if r["status"] == "pending":
                t = self._s.tickets[r["ticket_key"]]
                out.append({"id": r["id"], "ticket_key": r["ticket_key"], "target_stage": r["target_stage"],
                            "project_key": t.project_key, "jira_status": t.jira_status})
        return out

    def get(self, item_id):
        return next((dict(r) for r in self._s.outbox if r["id"] == item_id), None)

    def _row(self, item_id):
        return next(r for r in self._s.outbox if r["id"] == item_id)

    def update(self, item_id, status, now, detail=None, attempts_bump=False):
        r = self._row(item_id)
        r.update(status=status, detail=detail, updated_at=now, attempts=r.get("attempts", 0) + (1 if attempts_bump else 0))

    def mark_sent(self, item_id, now, response_json):
        self._row(item_id).update(status="sent", jira_response=response_json, updated_at=now)

    def listing(self, status=None):
        return [dict(r) for r in self._s.outbox if status is None or r["status"] == status]

    def for_ticket(self, key):
        return [dict(r) for r in self._s.outbox if r["ticket_key"] == key]

    def pending_count(self):
        return sum(1 for r in self._s.outbox if r["status"] == "pending")


class MemoryBoardUnitOfWork:
    """Rolls the state back if the block raises, like a database transaction."""

    def __init__(self, state: MemoryBoardState):
        self.state = state
        self.tickets = MemoryTicketRepository(state)
        self.mappings = MemoryStageMappings(state)
        self.outbox = MemoryOutboxQueue(state)
        self._snapshot = None

    def __enter__(self):
        self._snapshot = copy.deepcopy(self.state.__dict__)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.state.__dict__.clear()
            self.state.__dict__.update(self._snapshot)
