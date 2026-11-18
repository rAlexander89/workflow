"""One agent run: what it is told, what environment its gateway gets, and what came back."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

STAGE_NOTES = {
    "in_refinement": ("This is refinement. Product and Staff take turns on this ticket. Product shapes the work "
                      "with the user's intent; Staff checks it technically. Staff moves the ticket to Ready for dev "
                      "when the two agree. Until then, leave your reasons in a comment for the other agent."),
    "regression": ("This is the regression stage on dev. Run the regression against the dev app. "
                   "Approvals from earlier stages do not count here. If the regression passes, "
                   "call approve_ticket to record it. If it fails, flag_ticket with what failed."),
}


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None  # None when the run was stopped for time
    timed_out: bool
    output_path: Path  # stream-json events, one per line
    final: dict | None  # the last "result" event, if the run produced one


@dataclass
class AgentResult:
    key: str
    stage: str
    role: str
    run: RunResult | None


@dataclass(frozen=True)
class AgentRunRequest:
    worktree: Path
    prompt: str
    system_prompt: str
    model: str
    run_dir: Path
    role: str
    gateway_env: dict[str, str] = field(default_factory=dict)
    access: bool = False
    timeout: float = 3600.0


def brief(detail: dict) -> str:
    """The task an agent is given: the ticket, its stage notes, and its recent comments."""
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


def compose_system_prompt(base: str, bot_email: str = "", credentials_path: str = "") -> str:
    """The role's prompt, with the agent's own app login added when it has one."""
    if not bot_email:
        return base
    return base.rstrip() + f"""

## Your app account
- You have your own login to the local app: {bot_email}.
- The password is in the credentials file at {credentials_path}. You cannot open that file.
- Call bot_login before you use the app, and bot_logout when you are done. bot_login reads the password for you and never shows it.
- Never print, paste, log, or write a password anywhere, and never ask for one.
"""


def gateway_env(*, role: str, key: str, db_path: str, worktree: Path, test_cmd: str, app_cmd: str, app_url: str,
                bot_email: str, credentials_path: str, regression_url: str, on_dev: bool,
                runner_secret: str = "") -> dict[str, str]:
    """The scope the gateway gets from the environment: who the agent is, and what it may reach.

    The runner secret goes to the dev deploy's hostname only. Prod is never configured here.
    """
    env = {
        "WF_ROLE": role, "WF_KEY": key, "WF_DB": db_path, "WF_WORKTREE": str(worktree),
        "WF_TEST_CMD": test_cmd, "WF_APP_CMD": app_cmd, "WF_APP_URL": app_url,
        "WF_BOT_EMAIL": bot_email, "WF_BOT_CREDENTIALS": credentials_path,
    }
    if runner_secret and regression_url:
        env["WF_RUNNER_SECRET"] = runner_secret
        env["WF_RUNNER_SECRET_HOST"] = urlsplit(regression_url).hostname or ""
    if on_dev:
        env["WF_BOT_APP"] = regression_url  # the bot signs in to the same app it tests
    return env
