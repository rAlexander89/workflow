"""Read a pull request's state and CI checks from GitHub, through the `gh` CLI.

Read-only. Nothing here opens, merges, or changes a PR.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Callable

from wf.domain.delivery.checks import rollup_state as checks_state  # noqa: F401

_cache: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 30


def pr_status(url: str, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
              now: Callable[[], float] = time.time) -> dict:
    hit = _cache.get(url)
    if hit and now() - hit[0] < CACHE_SECONDS:
        return hit[1]
    from wf import forge
    f = forge.for_url(url)
    if isinstance(f, forge.GitHub):
        f = forge.GitHub(runner=lambda argv, cwd: runner(argv, capture_output=True, text=True, check=False))
    else:
        f = forge.GitLab(host=f.host, runner=lambda argv, cwd: runner(argv, capture_output=True, text=True, check=False))
    result = f.pr_status(url)
    _cache[url] = (now(), result)
    return result
