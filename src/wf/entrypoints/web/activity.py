"""The activity log and the one-at-a-time background runner behind the pages.

The use cases see only the ActivityLog and TaskRunner ports; this is what the web app gives them.
"""

from __future__ import annotations

import threading
import time
import traceback

_lock = threading.Lock()
_state: dict = {"running": None, "log": [], "last": None}


def log(msg: str) -> None:
    with _lock:
        _state["log"] = (_state["log"] + [f"{time.strftime('%H:%M:%S')}  {msg}"])[-30:]


def start(label: str, fn) -> bool:
    """Run one action at a time in the background. Returns False if one is already running."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = label
    log(f"started: {label}")

    def work():
        try:
            fn()
            with _lock:
                _state["last"] = f"{label}: done"
            log(f"finished: {label}")
        except Exception as e:  # shown on the page; the thread must not die silently
            with _lock:
                _state["last"] = f"{label}: failed: {e}"
            log(f"failed: {label}: {e}")
            log(traceback.format_exc(limit=2).strip().splitlines()[-1])
        finally:
            with _lock:
                _state["running"] = None

    threading.Thread(target=work, daemon=True).start()
    return True


def snapshot() -> dict:
    with _lock:
        return {"running": _state["running"], "last": _state["last"], "log": list(_state["log"])}


class Activity:
    """ActivityLog and TaskRunner over the functions above."""

    def log(self, message: str) -> None:
        log(message)

    def start(self, label: str, work) -> bool:
        return start(label, work)

    def busy(self) -> bool:
        with _lock:
            return bool(_state["running"])
