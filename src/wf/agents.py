"""Agents: run the role that owns a ticket's stage, and loop over the board.

Each run is one `claude -p` process in the ticket's branch worktree, connected to the
gateway (wf.gateway_server) with its scope set in the environment. The role's prompt
from prompts/ is the system prompt. The ticket is the task.

Every ticket has one branch, `agent/<KEY>`, cut from the internal main. All roles work on
that branch: dev writes to it, review and QA check it, and the merge gate reads it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from wf import board, claude_run
from wf.adapters.persistence.sqlite.board_queries import SqliteBoardQueries
from wf.adapters.persistence.sqlite.pause import SqlitePauseState
from wf.adapters.workspace.git import GitWorkspace
from wf.application.delivery.pipeline import PipelineService
from wf.domain.delivery import roles
from wf.domain.delivery.roles import (  # noqa: F401
    DONE_FOR_BLOCKING, MAX_REFINEMENT_ROUNDS, MAX_RUNS_PER_STAGE, PAUSABLE, REFINEMENT_STAGES, STAGE_ROLE,
    missing_approvals, next_refinement_role, refinement_limit_reached, run_limit_reached,
)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

from wf.adapters.persistence.sqlite.agent_runs import SCHEMA, SqliteAgentRuns  # noqa: E402,F401


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _runs(conn: sqlite3.Connection) -> SqliteAgentRuns:
    return SqliteAgentRuns(conn)


def runs_for(conn: sqlite3.Connection, key: str) -> list[dict]:
    """All agent runs on the ticket, newest first."""
    return _runs(conn).runs_for(key)


def last_exit_code(conn: sqlite3.Connection, key: str, role: str | None = None) -> int | None:
    """The exit code of the newest run (of this role, if given), or None if none ran."""
    return _runs(conn).last_exit_code(key, role)


# --- git ----------------------------------------------------------------------------

def branch_for(key: str) -> str:
    return f"agent/{key}"


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_worktree(repo: Path, key: str, worktrees: Path) -> Path:
    """The ticket's worktree on its branch, created from the internal main if needed."""
    branch = branch_for(key)
    worktrees = worktrees.resolve()
    path = worktrees / key
    if path.exists():
        return path
    worktrees.mkdir(parents=True, exist_ok=True)
    branches = _git(["branch", "--list", branch], repo)
    if branches:
        _git(["worktree", "add", str(path), branch], repo)
    else:
        has_origin = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "origin/main"], cwd=repo,
                                    capture_output=True).returncode == 0
        base = "origin/main" if has_origin else "main"
        _git(["worktree", "add", "-b", branch, str(path), base], repo)
    return path


def sync_with_main(repo: Path, worktree: Path) -> None:
    """Start from the latest main: fetch GitHub's main and rebase the ticket's branch onto it.

    Raises if the fetch fails or the rebase conflicts (the rebase is undone first). The branch
    is not changed in that case.
    """
    has_origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo,
                                capture_output=True).returncode == 0
    if not has_origin:
        return  # a repo with no hosted remote: its local main is the main
    fetch = subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=repo, capture_output=True, text=True)
    if fetch.returncode != 0:
        raise RuntimeError(f"could not fetch the latest main: {fetch.stderr.strip()[-200:]}")
    rebase = subprocess.run(["git", "rebase", "origin/main"], cwd=worktree, capture_output=True, text=True)
    if rebase.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=worktree, capture_output=True)
        raise RuntimeError("could not rebase onto the latest main; a person needs to resolve the conflict")


def diff_range(repo: Path, key: str) -> str:
    """What to diff for a ticket: its changes as merged into main, or its branch against main."""
    proc = subprocess.run(["git", "log", "main", "--merges", "--format=%H", "-1",
                           f"--grep=^Merge {branch_for(key)} "], cwd=repo, capture_output=True, text=True)
    merge_commit = proc.stdout.strip()
    if merge_commit:
        return f"{merge_commit}^1..{merge_commit}"
    return f"main...{branch_for(key)}"


def head_of(worktree: Path) -> str:
    return _git(["rev-parse", "HEAD"], worktree)


# --- runs ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    key: str
    stage: str
    role: str
    run: claude_run.RunResult | None


STAGE_NOTES = {
    "in_refinement": ("This is refinement. Product and Staff take turns on this ticket. Product shapes the work "
                      "with the user's intent; Staff checks it technically. Staff moves the ticket to Ready for dev "
                      "when the two agree. Until then, leave your reasons in a comment for the other agent."),
    "regression": ("This is the regression stage on dev. Run the regression against the dev app. "
                   "Approvals from earlier stages do not count here. If the regression passes, "
                   "call approve_ticket to record it. If it fails, flag_ticket with what failed."),
}


def brief(detail: dict) -> str:
    t = detail["ticket"]
    lines = [
        f"Ticket {t['key']}: {t['summary']}",
        f"Status in Jira: {t['jira_status']}. Internal stage: {t['stage']}.",
        "",
        "Description:",
        t.get("description") or "(none)",
    ]
    if t.get("stage") in STAGE_NOTES:
        lines += ["", STAGE_NOTES[t["stage"]]]
    if t.get("acceptance"):
        lines += ["", "Acceptance criteria:", t["acceptance"]]
    comments = [c for c in detail.get("comments", []) if c.get("body")]
    if comments:
        lines += ["", "Comments:"] + [f"- {c['author']}: {c['body']}" for c in comments[-10:]]
    return "\n".join(lines)


def system_prompt(role: str, bot_email: str = "", credentials_path: str = "") -> str:
    path = PROMPTS_DIR / f"{role}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt for role {role!r} at {path}")
    text = path.read_text()
    if not bot_email:
        return text
    return text.rstrip() + f"""

## Your app account
- You have your own login to the local app: {bot_email}.
- The password is in the credentials file at {credentials_path}. You cannot open that file.
- Call bot_login before you use the app, and bot_logout when you are done. bot_login reads the password for you and never shows it.
- Never print, paste, log, or write a password anywhere, and never ask for one.
"""


def run_agent(
    conn: sqlite3.Connection, role: str, key: str, *, repo: Path, runs_dir: Path,
    access: bool, model: str, test_cmd: str = "", app_cmd: str = "", app_url: str = "", claude_bin: str | None = None,
    runner: claude_run.Runner | None = None, timeout: float = 3600.0,
    clock: Callable[[], float] = time.time, bot_domain: str = "", bot_credentials: str = "",
    regression_url: str = "",
) -> AgentResult:
    ensure_schema(conn)
    stage_now = board.get_ticket(conn, key)
    on_dev = bool(regression_url) and stage_now is not None and stage_now.stage == "regression"
    if on_dev:
        app_url = regression_url  # the final regression runs against the deployed dev app
    bot_email = f"{role}@{bot_domain}" if bot_domain else ""
    cred_path = bot_credentials if bot_domain else ""
    detail = board.ticket_detail(conn, key)
    if detail is None:
        raise board.BoardError(f"no ticket {key}")
    stage = detail["ticket"]["stage"]
    worktree = ensure_worktree(repo, key, runs_dir / "worktrees")
    if role == "dev":
        try:
            sync_with_main(repo, worktree)
        except RuntimeError as e:
            board.set_flag(conn, key, "blocked", "harness", str(e), clock=clock)
            return AgentResult(key, stage, role, None)
    run_dir = runs_dir / "runs" / key / f"{role}-{int(clock())}"
    env = {
        "WF_ROLE": role, "WF_KEY": key, "WF_DB": board.database_path(conn),
        "WF_WORKTREE": str(worktree), "WF_TEST_CMD": test_cmd, "WF_APP_CMD": app_cmd, "WF_APP_URL": app_url,
    }
    gateway = [sys.executable, "-m", "wf.gateway_server"]
    env.update({"WF_BOT_EMAIL": bot_email, "WF_BOT_CREDENTIALS": cred_path})
    if os.environ.get("WF_RUNNER_SECRET") and regression_url:
        # The secret goes to the dev deploy's hostname only. Prod is never configured here.
        from urllib.parse import urlsplit
        env["WF_RUNNER_SECRET"] = os.environ["WF_RUNNER_SECRET"]
        env["WF_RUNNER_SECRET_HOST"] = urlsplit(regression_url).hostname or ""
    if on_dev:
        env["WF_BOT_APP"] = regression_url  # the bot signs in to the same app it tests
    result = claude_run.run(
        worktree=worktree, prompt=brief(detail), system_prompt=system_prompt(role, bot_email, cred_path), model=model,
        run_dir=run_dir, profile=role, gateway_command=gateway, access=access, gateway_env=env,
        timeout=timeout, claude_bin=claude_bin, runner=runner,
    )
    _runs(conn).record(key, stage, role, int(clock()), result.exit_code, result.timed_out, str(result.output_path))
    return AgentResult(key, stage, role, result)


def refinement_role(conn: sqlite3.Connection, key: str) -> str:
    """Whose turn it is in refinement: product goes first, then staff, and so on.

    Staff moves the ticket out of refinement when the two agree. Until then each agent's
    run hands the turn to the other, and MAX_REFINEMENT_ROUNDS caps the product rounds.
    """
    return next_refinement_role(_runs(conn).last_role_at(key, "in_refinement"))


def runs_at(conn: sqlite3.Connection, key: str, stage: str) -> int:
    """How many runs in a row have failed here (non-zero exit or timed out), most recent first.

    A run that succeeds resets the count, so work keeps going while the agent is making
    progress. Only repeated failures count toward the stop: that is a real obstacle.
    """
    return _runs(conn).failed_in_a_row(key, stage)


def prod_ready(conn: sqlite3.Connection, key: str, repo: Path) -> bool:
    """Regression passed on the current commit (and performance, if the dev asked for it).

    Only then does the Deploy to prod button appear. Approvals count only after the ticket
    entered the stage they belong to.
    """
    proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch_for(key)], cwd=repo,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    head = proc.stdout.strip()
    if "qa" not in board.approvals_since_stage(conn, key, head, "regression"):
        return False
    if board.perf_requested(conn, key):
        return "performance" in board.approvals_since_stage(conn, key, head, "performance")
    return True


def unmet_blockers(conn: sqlite3.Connection, key: str, stage_of: dict[str, str]) -> list[str]:
    """Prerequisites of this ticket that are not on dev yet."""
    return roles.unmet_blockers(board.blockers(conn, key), stage_of)


class _Deploys:
    """DeployObserver over a function, so callers can inject how a dev deploy is detected."""

    def __init__(self, dev_deployed: Callable[[str], bool]):
        self._dev_deployed = dev_deployed

    def dev_deployed(self, key: str) -> bool:
        return self._dev_deployed(key)


def pipeline_service(conn: sqlite3.Connection, repo: Path | None = None,
                     dev_deployed: Callable[[str], bool] | None = None) -> PipelineService:
    """The pipeline use cases wired to this board connection and repo."""
    from wf import observe

    return PipelineService(
        commands=board._commands(conn), queries=SqliteBoardQueries(conn), pause=SqlitePauseState(conn),
        runs=SqliteAgentRuns(conn), workspace=GitWorkspace(repo or Path(".")),
        deploys=_Deploys(dev_deployed or (lambda key: observe.dev_deployed(conn, key))))


def advance_pipeline(conn: sqlite3.Connection, *, repo: Path, dev_deployed=None) -> list[str]:
    """Move tickets along the steps that need no agent judgement. Returns the keys moved.

    - an approval at the branch head is the review, so the ticket goes on to QA
    - a merged ticket goes to regression once its PR has merged on GitHub (so regression
      runs on dev, after the deploy). pr_merged(key) says whether it has
    - a regression pass goes to performance if the dev asked for a check
    - a ticket in the active sprint's backlog is ready for development: it goes to ready_for_dev,
      and the dev agent takes it from there
    Deploying to prod is a person's step; nothing here moves a ticket past regression.
    While work is paused, no ticket moves.
    """
    return pipeline_service(conn, repo, dev_deployed).advance()


def _finished_and_unchanged(conn: sqlite3.Connection, key: str, stage: str) -> bool:
    """True when the last run in this stage succeeded and nothing has happened to the ticket since."""
    return pipeline_service(conn)._finished_and_unchanged(key, stage)


def idle_reason(conn: sqlite3.Connection, key: str) -> dict | None:
    """Why a ticket is not moving, or None when the next pass will pick it up.

    Returns {"why": text, "can_run": bool}. can_run means a person can start the stage's agent
    now with the Run again button. It is False when something else has to change first.
    """
    return pipeline_service(conn).idle_reason(key)


def tick(
    conn: sqlite3.Connection, *, repo: Path, runs_dir: Path, access: bool, model: str,
    test_cmd: str = "", app_cmd: str = "", app_url: str = "", bot_domain: str = "", bot_credentials: str = "",
    regression_url: str = "",
    dry_run: bool = True, runner: claude_run.Runner | None = None,
    claude_bin: str | None = None, clock: Callable[[], float] = time.time,
) -> list[dict]:
    """One pass over the board: run the role for each ticket that needs one.

    Work order: a ticket already in development first (a kicked-back one), then ready tickets in
    the sprint's work order (unordered last). One dev at a time: a ready ticket is picked up only
    when no ticket is in development, and only when its prerequisites are on dev. A ticket in
    refinement gets at most MAX_REFINEMENT_ROUNDS product rounds before a person is asked.
    dry_run lists the work without running it. While work is paused, nothing is listed or run.
    """
    ensure_schema(conn)
    actions: list[dict] = []
    for planned in pipeline_service(conn, repo).plan_pass():
        action = planned.to_dict()
        if planned.flag_needs_human:
            if not dry_run:
                board.set_flag(conn, planned.key, "needs-human", "harness", planned.skipped, clock=clock)
        elif planned.skipped is None and not dry_run:
            if planned.stage == "ready_for_dev":
                board.move_stage(conn, planned.key, "in_dev", "dev", "dev pickup")
                action["stage"] = "in_dev"
            res = run_agent(conn, planned.role, planned.key, repo=repo, runs_dir=runs_dir, access=access,
                            model=model, test_cmd=test_cmd, app_cmd=app_cmd, app_url=app_url, claude_bin=claude_bin,
                            runner=runner, clock=clock, regression_url=regression_url,
                            bot_domain=bot_domain, bot_credentials=bot_credentials)
            action["exit_code"] = res.run.exit_code if res.run else None
        actions.append(action)
    return actions
