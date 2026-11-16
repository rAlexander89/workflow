"""Read a pull request's state and CI checks from GitHub, through the `gh` CLI.

Read-only. Nothing here opens, merges, or changes a PR.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Callable

FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_cache: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 30


def checks_state(rollup: list[dict]) -> str:
    """'none', 'pending', 'failed', or 'passed' for a PR's check runs."""
    if not rollup:
        return "none"
    if any((c.get("conclusion") or "").upper() in FAILED for c in rollup):
        return "failed"
    if any((c.get("status") or "").upper() != "COMPLETED" for c in rollup):
        return "pending"
    return "passed"


def pr_status(url: str, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
              now: Callable[[], float] = time.time) -> dict:
    hit = _cache.get(url)
    if hit and now() - hit[0] < CACHE_SECONDS:
        return hit[1]
    proc = runner(["gh", "pr", "view", url, "--json", "state,mergedAt,mergeStateStatus,statusCheckRollup"],
                  capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        result = {"ok": False, "error": (proc.stderr or "gh failed").strip()[-200:]}
    else:
        data = json.loads(proc.stdout)
        result = {
            "ok": True,
            "state": data.get("state", ""),
            "merged": bool(data.get("mergedAt")),
            "checks": checks_state(data.get("statusCheckRollup") or []),
            "merge_state": data.get("mergeStateStatus", ""),
        }
    _cache[url] = (now(), result)
    return result
