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

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
MAX_RUNS_PER_STAGE = 3  # loop guard: a ticket that bounces this often needs a human

# Which role runs in each stage. Stages not listed have no agent.
STAGE_ROLE: dict[str, str] = {
    "in_refinement": "product",  # product and staff take turns here: see refinement_role
    "hotfix_review": "staff",
    "ready_for_dev": "dev",
    "in_dev": "dev",
    "pr_open": "pr_review",
    "pr_in_review": "pr_review",
    "ready_for_perf_review": "perf_review",
    "ready_for_qa": "qa",
    "merged": None,  # the harness moves it on to regression
    "regression": "qa",
    "performance": "performance",
}

REFINEMENT_STAGES = ("in_refinement",)
MAX_REFINEMENT_ROUNDS = 7  # product rounds without the staff bot's agreement: a person is asked
DONE_FOR_BLOCKING = ("merged", "regression", "performance", "deployed")  # a prerequisite is on dev
PAUSABLE = ("in_dev", "pr_open", "pr_in_review")  # dev-owned work a kicked-back prerequisite interrupts


SCHEMA = """
create table if not exists agent_run (
    id          integer primary key autoincrement,
    key         text not null,
    stage       text not null,
    role        text not null,
    started_at  integer not null,
    exit_code   integer,
    timed_out   integer not null default 0,
    output_path text not null
);
create index if not exists agent_run_key on agent_run (key, stage);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


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
    stage_now = conn.execute("select stage from ticket where key = ?", (key,)).fetchone()
    on_dev = bool(regression_url) and stage_now is not None and stage_now[0] == "regression"
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
        "WF_ROLE": role, "WF_KEY": key, "WF_DB": str(conn.execute("pragma database_list").fetchone()[2]),
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
    conn.execute(
        "insert into agent_run (key, stage, role, started_at, exit_code, timed_out, output_path) "
        "values (?, ?, ?, ?, ?, ?, ?)",
        (key, stage, role, int(clock()), result.exit_code, int(result.timed_out), str(result.output_path)),
    )
    conn.commit()
    return AgentResult(key, stage, role, result)


def refinement_role(conn: sqlite3.Connection, key: str) -> str:
    """Whose turn it is in refinement: product goes first, then staff, and so on.

    Staff moves the ticket out of refinement when the two agree. Until then each agent's
    run hands the turn to the other, and MAX_REFINEMENT_ROUNDS caps the product rounds.
    """
    ensure_schema(conn)
    last = conn.execute(
        "select role from agent_run where key = ? and stage = 'in_refinement' order by id desc limit 1",
        (key,)).fetchone()
    if last is None or last["role"] == "staff":
        return "product"
    return "staff"


def runs_at(conn: sqlite3.Connection, key: str, stage: str) -> int:
    """How many runs in a row have failed here (non-zero exit or timed out), most recent first.

    A run that succeeds resets the count, so work keeps going while the agent is making
    progress. Only repeated failures count toward the stop: that is a real obstacle.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "select exit_code, timed_out from agent_run where key = ? and stage = ? order by id desc",
        (key, stage)).fetchall()
    streak = 0
    for r in rows:
        if r["timed_out"] or r["exit_code"] != 0:
            streak += 1
        else:
            break
    return streak


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


def _sql_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def unmet_blockers(conn: sqlite3.Connection, key: str, stage_of: dict[str, str]) -> list[str]:
    """Prerequisites of this ticket that are not on dev yet."""
    return [b for b in board.blockers(conn, key) if stage_of.get(b) not in DONE_FOR_BLOCKING]


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
    from wf import observe, pause

    if pause.is_paused(conn):
        return []

    # A sprint ticket goes into refinement (product and staff agree the work) before a dev takes it.
    moved: list[str] = []
    for row in conn.execute(
            "select t.key from ticket t join sprint s on s.id = t.sprint_id "
            "where t.stage = 'backlog' and s.state = 'active' and t.removed_at is null order by t.key").fetchall():
        board.move_stage(conn, row["key"], "in_refinement", "harness", "in the active sprint: refine it")
        moved.append(row["key"])

    # A prerequisite sent back to dev pauses the work that depends on it.
    for row in conn.execute(
            "select distinct t.key, b.key as blocker from ticket t "
            "join sprint s on s.id = t.sprint_id "
            "join ticket_link l on l.ticket_key = t.key and l.link_type = 'is blocked by' "
            "join ticket b on b.key = l.other_key "
            f"where s.state = 'active' and t.removed_at is null and t.stage in {_sql_list(PAUSABLE)} "
            "and b.stage = 'in_dev' order by t.key").fetchall():
        board.move_stage(conn, row["key"], "ready_for_dev", "harness",
                         f"paused: prerequisite {row['blocker']} was sent back to dev")
        moved.append(row["key"])

    if dev_deployed is None:
        dev_deployed = lambda key: observe.dev_deployed(conn, key)  # noqa: E731
    ensure_schema(conn)
    rows = conn.execute(
        "select key, stage from ticket where stage in ('pr_open', 'pr_in_review', 'merged', 'regression', 'performance') "
        "and removed_at is null").fetchall()
    for row in rows:
        key, stage = row["key"], row["stage"]
        if stage == "merged":
            if dev_deployed(key):
                board.move_stage(conn, key, "regression", "harness", "deployed to dev; regression on dev")
                moved.append(key)
            continue
        if stage in ("regression", "performance"):
            proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch_for(key)], cwd=repo,
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                continue
            head = proc.stdout.strip()
            if stage == "regression" and "qa" in board.approvals_since_stage(conn, key, head, "regression"):
                if board.perf_requested(conn, key):
                    board.move_stage(conn, key, "performance", "harness", "regression passed; performance requested")
                else:
                    board.move_stage(conn, key, "deployed", "harness", f"regression passed at {head[:8]}; deployed to dev")
                moved.append(key)
            elif stage == "performance" and "performance" in board.approvals_since_stage(conn, key, head, "performance"):
                board.move_stage(conn, key, "deployed", "harness", f"performance passed at {head[:8]}")
                moved.append(key)
            continue
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch_for(key)], cwd=repo,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            continue
        head = proc.stdout.strip()
        if stage in ("pr_open", "pr_in_review") and "pr_review" in board.approvals_at(conn, key, head):
            board.move_stage(conn, key, "ready_for_perf_review", "harness", f"PR review approved at {head[:8]}")
            moved.append(key)
    return moved


def _finished_and_unchanged(conn: sqlite3.Connection, key: str, stage: str) -> bool:
    """True when the last run in this stage succeeded and nothing has happened to the ticket since.

    An event by anyone other than that run's own role counts as a change, so a review that sends the
    ticket back to this stage makes the stage run again.
    """
    last = conn.execute(
        "select role, exit_code, timed_out, started_at from agent_run where key = ? and stage = ? "
        "order by id desc limit 1", (key, stage)).fetchone()
    if last is None or last["timed_out"] or last["exit_code"] != 0:
        return False
    changed = conn.execute(
        "select 1 from ticket_event where ticket_key = ? and at >= ? and actor != ? limit 1",
        (key, last["started_at"], last["role"])).fetchone()
    return changed is None


def idle_reason(conn: sqlite3.Connection, key: str) -> dict | None:
    """Why a ticket is not moving, or None when the next pass will pick it up.

    Returns {"why": text, "can_run": bool}. can_run means a person can start the stage's agent
    now with the Run again button. It is False when something else has to change first.
    """
    from wf import pause

    ensure_schema(conn)
    row = conn.execute(
        "select t.stage, t.flags, s.state as sprint_state from ticket t "
        "left join sprint s on s.id = t.sprint_id where t.key = ? and t.removed_at is null", (key,)).fetchone()
    if row is None or row["stage"] is None:
        return None
    stage = row["stage"]
    flags = json.loads(row["flags"] or "[]")
    role = refinement_role(conn, key) if stage in REFINEMENT_STAGES else STAGE_ROLE.get(stage)

    if pause.is_paused(conn):
        return {"why": "Work is paused. It resumes when you press Resume work.", "can_run": False}
    if flags:
        return {"why": f"Flagged: {', '.join(flags)}. Clear the flag first.", "can_run": False}
    if role is None:
        return None  # nothing is expected to happen here (backlog, done): no notice
    if row["sprint_state"] != "active":
        return {"why": "Not in an active sprint, so no agent picks it up.", "can_run": False}
    if stage in REFINEMENT_STAGES:
        rounds = conn.execute("select count(*) from agent_run where key = ? and role = 'product'", (key,)).fetchone()[0]
        if rounds >= MAX_REFINEMENT_ROUNDS:
            return {"why": f"{rounds} refinement rounds without agreement. A person has to decide.", "can_run": True}
    elif runs_at(conn, key, stage) >= MAX_RUNS_PER_STAGE:
        return {"why": f"{MAX_RUNS_PER_STAGE} runs in a row failed here. A person has to look at the last run.",
                "can_run": True}
    elif stage == "ready_for_dev":
        stage_of = {r["key"]: r["stage"] for r in conn.execute("select key, stage from ticket where removed_at is null")}
        waiting = unmet_blockers(conn, key, stage_of)
        if waiting:
            return {"why": f"Waiting for {', '.join(waiting)} to reach dev first.", "can_run": False}
    elif stage not in REFINEMENT_STAGES and _finished_and_unchanged(conn, key, stage):
        return {"why": f"The {role} agent finished its last run here and nothing has changed since. "
                       "It won't run again until something changes or you run it.", "can_run": True}
    return None


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
    from wf import pause

    if pause.is_paused(conn):
        return []
    ensure_schema(conn)
    rows = conn.execute(
        """select t.key, t.stage, t.flags, t.sort_order, s.state as sprint_state
             from ticket t left join sprint s on s.id = t.sprint_id
            where t.removed_at is null and t.stage is not null"""
    ).fetchall()
    stage_of = {r["key"]: r["stage"] for r in rows}
    dev_busy = next((r["key"] for r in rows if r["stage"] == "in_dev" and r["sprint_state"] == "active"), None)
    order = sorted(rows, key=lambda r: (r["stage"] != "in_dev", r["sort_order"] is None,
                                        r["sort_order"] or 0, r["key"]))
    actions: list[dict] = []
    for row in order:
        key, stage = row["key"], row["stage"]
        role = STAGE_ROLE.get(stage)
        if stage in REFINEMENT_STAGES:
            role = refinement_role(conn, key)  # refinement alternates; the round limit below ends it
        if role is None or row["sprint_state"] != "active" or json.loads(row["flags"] or "[]"):
            continue
        if stage not in REFINEMENT_STAGES and _finished_and_unchanged(conn, key, stage):
            continue  # the last run here succeeded and nothing has changed since: wait for a change
        action = {"key": key, "stage": stage, "role": role, "skipped": None}
        if stage in REFINEMENT_STAGES:
            rounds = conn.execute("select count(*) from agent_run where key = ? and role = 'product'",
                                  (key,)).fetchone()[0]
            if rounds >= MAX_REFINEMENT_ROUNDS:
                action["skipped"] = f"{rounds} refinement rounds without agreement; needs a human"
                if not dry_run:
                    board.set_flag(conn, key, "needs-human", "harness", action["skipped"], clock=clock)
                actions.append(action)
                continue
        elif runs_at(conn, key, stage) >= MAX_RUNS_PER_STAGE:
            action["skipped"] = "run limit reached for this stage; needs a human"
            actions.append(action)
            continue
        if stage == "ready_for_dev":
            if dev_busy is not None:
                action["skipped"] = f"dev is busy with {dev_busy}"
                actions.append(action)
                continue
            waiting = unmet_blockers(conn, key, stage_of)
            if waiting:
                action["skipped"] = f"waiting for {', '.join(waiting)} to merge to dev"
                actions.append(action)
                continue
        if stage == "in_dev" and dev_busy != key:
            action["skipped"] = f"dev is busy with {dev_busy}"
            actions.append(action)
            continue
        if not dry_run:
            if stage == "ready_for_dev":
                board.move_stage(conn, key, "in_dev", "dev", "dev pickup")
                action["stage"] = "in_dev"
            res = run_agent(conn, role, key, repo=repo, runs_dir=runs_dir, access=access,
                            model=model, test_cmd=test_cmd, app_cmd=app_cmd, app_url=app_url, claude_bin=claude_bin, runner=runner, clock=clock,
                            regression_url=regression_url,
                            bot_domain=bot_domain, bot_credentials=bot_credentials)
            action["exit_code"] = res.run.exit_code if res.run else None
        if stage == "ready_for_dev":
            dev_busy = key  # the dev holds this ticket now; no other ready ticket starts this pass
        actions.append(action)
    return actions
