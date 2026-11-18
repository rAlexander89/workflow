"""The loop that wakes the pipeline: every 15 seconds, or sooner when a webhook says something changed."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from wf.bootstrap import Container
from wf.entrypoints.web import activity


@dataclass
class Signals:
    wake: threading.Event = field(default_factory=threading.Event)  # a GitHub webhook: check now, not in 15s
    jira_changed: threading.Event = field(default_factory=threading.Event)  # a Jira webhook: read Jira now


def start(container: Container, signals: Signals, interval: float = 15.0) -> threading.Thread:
    """Run the harness cycle in a background thread: start the next pass by itself when a ticket is ready,
    for example right after its flag clears."""

    def loop():
        while True:
            signals.wake.wait(interval)
            signals.wake.clear()
            try:
                settings = container.settings()
                jira_changed = signals.jira_changed.is_set()
                with container.session() as s:
                    waiting = s.cycle(settings, activity.Activity()).run_once(jira_changed=jira_changed)
                if jira_changed and not waiting:
                    signals.jira_changed.clear()  # the sync started; otherwise it stays set for the next cycle
            except Exception as e:  # never let the loop die
                activity.log(f"auto pass check failed: {e}")

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
