"""The Jira outbox: decide which queued board moves to send, and record what happened.

The Atlassian MCP lives in the user's Claude session, so the Jira agent runs there in
three steps. The Python side decides; the session only calls Jira.

1. `pending()` lists the tickets that have queued moves.
2. The session reads each ticket's current Jira status and allowed transitions through
   the MCP, and writes them to a JSON file of observations.
3. `plan(observations)` decides each queued move:
     - send     : Jira allows the move. The item is marked `sending`, so it is never
                  sent twice. The session calls the transition through the MCP.
     - skipped  : Jira is already in the target status.
     - conflict : Jira's status changed since our last sync. Not forced. The ticket is
                  flagged for the user.
     - blocked  : Jira has no transition to the target from its current status.
4. The session reports each result with `ack(results)`. A sent item is final.
   A failed or stuck item can be put back with `retry(id)`.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from wf.application.ports.board import BoardUnitOfWork
from wf.domain.board.errors import BoardError

RETRYABLE = ("blocked", "conflict", "failed", "sending")


class OutboxService:
    def __init__(self, unit_of_work: Callable[[], BoardUnitOfWork], clock: Callable[[], float]):
        self._uow = unit_of_work
        self._clock = clock

    def pending(self) -> list[dict]:
        with self._uow() as uow:
            return uow.outbox.pending()

    def listing(self, status: str | None = None) -> list[dict]:
        with self._uow() as uow:
            return uow.outbox.listing(status)

    def for_ticket(self, key: str) -> list[dict]:
        with self._uow() as uow:
            return uow.outbox.for_ticket(key)

    def pending_count(self) -> int:
        with self._uow() as uow:
            return uow.outbox.pending_count()

    def plan(self, observations: dict[str, Any]) -> dict:
        """Decide what to send, given the Jira state the session observed.

        `observations` maps issue key -> {"status": <current Jira status name>,
        "transitions": [{"id": <transition id>, "to": <status name it leads to>}, ...]}.
        """
        now = int(self._clock())
        result: dict[str, Any] = {"need": [], "send": [], "decided": {}}

        def count(status: str) -> None:
            result["decided"][status] = result["decided"].get(status, 0) + 1

        with self._uow() as uow:
            for item in uow.outbox.pending():
                key = item["ticket_key"]
                target_status = uow.mappings.status_for(item["project_key"], item["target_stage"])
                if target_status is None:
                    uow.outbox.update(item["id"], "blocked", now, "the stage has no mapped Jira status")
                    count("blocked")
                    continue

                obs = observations.get(key)
                if obs is None:
                    result["need"].append(key)
                    continue
                current = obs["status"]

                if current == target_status:
                    uow.outbox.update(item["id"], "skipped", now, f"already in {target_status!r}")
                    uow.tickets.set_jira_status(key, current)
                    count("skipped")
                elif current != item["jira_status"]:
                    uow.outbox.update(item["id"], "conflict", now,
                                      f"Jira status is {current!r}, last seen {item['jira_status']!r}; not forced")
                    ticket = uow.tickets.get(key)
                    if ticket.flag("needs-human", "outbox",
                                   f"Jira changed to {current!r} since last sync; a move to {target_status!r} is waiting",
                                   now):
                        uow.tickets.save(ticket)
                    count("conflict")
                else:
                    transition = next((t for t in obs.get("transitions", []) if t.get("to") == target_status), None)
                    if transition is None:
                        uow.outbox.update(item["id"], "blocked", now,
                                          f"Jira has no transition from {current!r} to {target_status!r}")
                        count("blocked")
                    else:
                        uow.outbox.update(item["id"], "sending", now, attempts_bump=True)
                        result["send"].append({"id": item["id"], "key": key,
                                               "transition_id": transition["id"], "to_status": target_status})
                        count("sending")
        return result

    def ack(self, results: list[dict]) -> dict:
        """Record what the session got back from Jira for each sent item."""
        now = int(self._clock())
        counts = {"sent": 0, "failed": 0, "ignored": 0}
        with self._uow() as uow:
            for r in results:
                row = uow.outbox.get(r["id"])
                if row is None or row["status"] != "sending":
                    counts["ignored"] += 1
                    continue
                if r.get("ok"):
                    status_after = r.get("jira_status") or row["target_stage"]
                    uow.outbox.mark_sent(r["id"], now, json.dumps(r.get("response")))
                    uow.tickets.set_jira_status(row["ticket_key"], status_after)
                    ticket = uow.tickets.get(row["ticket_key"])
                    ticket.sent_to_jira(status_after, now)
                    uow.tickets.save(ticket)
                    counts["sent"] += 1
                else:
                    uow.outbox.update(r["id"], "failed", now, str(r.get("error", "unknown error"))[:500])
                    counts["failed"] += 1
        return counts

    def retry(self, item_id: int) -> None:
        with self._uow() as uow:
            row = uow.outbox.get(item_id)
            if row is None:
                raise BoardError(f"no outbox item {item_id}")
            if row["status"] not in RETRYABLE:
                raise BoardError(
                    f"item {item_id} is {row['status']!r}; only {', '.join(RETRYABLE)} can be retried")
            uow.outbox.update(item_id, "pending", int(self._clock()), "retry requested")
