"""Jira work: read it into the board, send queued moves, link blocking tickets, and move tickets into a sprint.

Jira is reached only through the Tracker port, which runs in the user's own session. Every write goes through
the outbox, which decides what to send and records what happened.
"""

from __future__ import annotations

from typing import Callable

from wf.application.board.tickets import TicketCommands
from wf.application.ports.board import BoardQueries, BoardSync
from wf.application.ports.harness import ActivityLog, Tracker
from wf.application.tracker.outbox import OutboxService
from wf.domain.board.payload import SyncResult


class JiraService:
    def __init__(self, *, tracker: Tracker, sync: BoardSync, queries: BoardQueries, commands: TicketCommands,
                 outbox: OutboxService, log: ActivityLog, clock: Callable[[], float]):
        self._tracker = tracker
        self._sync = sync
        self._queries = queries
        self._commands = commands
        self._outbox = outbox
        self._log = log
        self._clock = clock

    def sync(self) -> SyncResult:
        """Read the user's tickets from Jira and apply them to the board."""
        payload = self._tracker.fetch_sync_payload()
        self._sync.carry_workflow_statuses(payload)
        result = self._sync.apply(payload, self._clock)
        counts = result.counts
        self._log.log(f"synced {len(payload['tickets'])} ticket(s) from Jira; "
                      f"{counts.get('tickets_created', 0)} new, {counts.get('tickets_updated', 0)} updated")
        for problem in result.problems:
            self._log.log(f"sync: {problem}")
        return result

    def send_queued(self) -> None:
        """Send the moves waiting in the outbox: read Jira's state, plan, send, and record the results."""
        items = self._outbox.pending()
        if not items:
            self._log.log("nothing queued for Jira")
            return
        observed = self._tracker.fetch_observations(sorted({i["ticket_key"] for i in items}))
        plan = self._outbox.plan(observed)
        for skipped in plan.get("need", []):
            self._log.log(f"{skipped}: Jira state not read; not sent")
        sends = [{"id": i["id"], "key": i["key"], "transition_id": i["transition_id"], "to_status": i["to_status"]}
                 for i in plan["send"]]
        for s in sends:
            self._log.log(f"will move {s['key']} to {s['to_status']}")
        results = self._tracker.execute_sends(sends)
        if results:
            self._log.log(f"Jira: {self._outbox.ack(results)}")

    def pending_links(self) -> list[tuple[str, str]]:
        return self._commands.pending_links()

    def link_pending(self, pairs: list[tuple[str, str]]) -> None:
        """Create the blocking links in Jira, then mark them as sent."""
        linked = self._tracker.create_blocking_links(pairs)
        self._commands.mark_linked(linked)
        self._log.log(f"linked in Jira: {', '.join(f'{p} blocks {d}' for p, d in linked)}")

    def active_sprint(self) -> dict | None:
        return self._queries.active_sprint()

    def move_tickets_to_sprint(self, keys: list[str], sprint: dict) -> None:
        moved = self._tracker.move_tickets_to_sprint(keys, sprint["id"])
        self._log.log(f"moved {len(moved)} ticket(s) to {sprint['name']} in Jira: {', '.join(moved)}")
        self.sync()

    def move_to_sprint(self, key: str, sprint: dict) -> None:
        self._tracker.move_to_sprint(key, sprint["id"])
        self._log.log(f"{key}: moved to {sprint['name']} in Jira")
        self.sync()
