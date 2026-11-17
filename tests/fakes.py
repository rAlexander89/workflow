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
