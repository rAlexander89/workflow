"""An in-process event bus: domain events are published inside the unit of work that produced them.

Handlers run before the transaction commits, so what a handler writes commits or rolls back with the change that
caused it.
"""

from __future__ import annotations

from typing import Callable

from wf.domain.board.events import DomainEvent

Handler = Callable[[DomainEvent, object], None]  # (event, the unit of work it was raised in)


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[tuple[type, Handler]] = []

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers.append((event_type, handler))

    def publish(self, events: list[DomainEvent], uow: object) -> None:
        for event in events:
            for event_type, handler in self._handlers:
                if isinstance(event, event_type):
                    handler(event, uow)
