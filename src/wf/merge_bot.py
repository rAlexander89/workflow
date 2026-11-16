"""The merge bot: decides whether a ticket's GitHub PR is good to merge, and says why not.

Good to merge means all of these hold:
- the PR is open on GitHub, not already merged or closed
- the internal PR reviewer and QA both approved the branch's current commit
- nothing is flagged on the ticket
- GitHub's checks have passed
- every commit the PR would add to GitHub's main carries the ticket key

The bot only reads. The auto pass calls merge_when_clear, which merges only when this says yes.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from wf import agents, board, github_status

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)  # why not, when ok is False


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def stray_commits(key: str, repo: Path, runner: Runner | None = None) -> tuple[bool, list[str]]:
    """(ok, stray): ok when every commit the branch adds to GitHub's main carries the ticket key."""
    run = runner or _run
    fetched = run(["git", "fetch", "-q", "origin", "main"], repo)
    if fetched.returncode != 0 and run(["git", "rev-parse", "--verify", "--quiet", "origin/main"], repo).returncode != 0:
        return False, ["could not compare with GitHub's main: no origin/main to compare against"]
    log = run(["git", "log", "--format=%s", f"origin/main..{agents.branch_for(key)}"], repo)
    if log.returncode != 0:
        return False, ["could not read the branch's commits"]
    stray = [l for l in log.stdout.splitlines() if l.strip() and key not in l]
    return (not stray), stray


def assess(conn: sqlite3.Connection, key: str, *, repo: Path, runner: Runner | None = None,
           status: Callable[[str], dict] | None = None) -> Verdict:
    run = runner or _run
    get = status or github_status.pr_status
    reasons: list[str] = []

    row = conn.execute("select flags, stage from ticket where key = ?", (key,)).fetchone()
    if row is None:
        return Verdict(False, [f"no ticket {key}"])
    if row["flags"] not in (None, "[]"):
        reasons.append("the ticket is flagged; a person has to clear it")

    pr = conn.execute("select detail from ticket_event where ticket_key = ? and kind = 'pr' "
                      "order by id desc limit 1", (key,)).fetchone()
    if pr is None:
        return Verdict(False, reasons + ["there is no GitHub PR for this ticket yet"])
    url = pr[0]

    st = get(url)
    if not st.get("ok"):
        reasons.append(f"could not read the PR from GitHub: {st.get('error', '')}")
    else:
        if st.get("merged"):
            reasons.append("the PR is already merged")
        elif st.get("state") != "OPEN":
            reasons.append(f"the PR is {st.get('state', 'not open').lower()}")
        if st.get("checks") == "pending":
            reasons.append("GitHub checks are still running")
        elif st.get("checks") == "failed":
            reasons.append("GitHub checks failed")
        elif st.get("checks") == "none":
            reasons.append("GitHub reports no checks on this PR")

    head_proc = run(["git", "rev-parse", "--verify", "--quiet", agents.branch_for(key)], repo)
    if head_proc.returncode != 0:
        reasons.append("the ticket's branch is missing")
    else:
        approvals = board.approvals_at(conn, key, head_proc.stdout.strip())
        missing = {"pr_review", "qa"} - approvals
        if missing:
            reasons.append(f"not approved on the current commit by: {', '.join(sorted(missing))}")

        ok, stray = stray_commits(key, repo, run)
        if not ok and stray and stray[0].startswith("could not"):
            reasons.append(stray[0])
        elif not ok:
            reasons.append(f"the PR would also add {len(stray)} commit(s) that are not {key}, "
                           f"starting with: {stray[0][:70]}")

    return Verdict(not reasons, reasons)
