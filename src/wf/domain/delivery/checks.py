"""Summarize a pull request's CI checks as 'none', 'pending', 'failed', or 'passed'."""

from __future__ import annotations

_ROLLUP_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_PAIR_FAILED = {"FAILURE", "ERROR", "CANCELED", "CANCELLED", "TIMED_OUT", "FAILED"}


def rollup_state(rollup: list[dict]) -> str:
    """From GitHub's status check rollup: dicts with `status` and `conclusion`."""
    if not rollup:
        return "none"
    if any((c.get("conclusion") or "").upper() in _ROLLUP_FAILED for c in rollup):
        return "failed"
    if any((c.get("status") or "").upper() != "COMPLETED" for c in rollup):
        return "pending"
    return "passed"


def pairs_state(conclusions: list[tuple[str, str]]) -> str:
    """From (status, conclusion) pairs, as a forge adapter reads them."""
    if not conclusions:
        return "none"
    if any((c or "").upper() in _PAIR_FAILED for _, c in conclusions):
        return "failed"
    if any((s or "").upper() not in ("COMPLETED", "SUCCESS", "SUCCESSFUL", "PASSED") for s, _ in conclusions):
        return "pending"
    return "passed"
