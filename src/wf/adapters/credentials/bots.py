"""Bot accounts: what each bot may and may not do, and where its login lives.

The bots are separate accounts in the project's app, one per role. Passwords are generated
on this machine and written to a credentials file outside any repository, with owner-only
permissions. They are never printed or stored in workflow.json.
"""

from __future__ import annotations

import os
import secrets
import string
from pathlib import Path
from typing import Callable

ROLES = ("dev", "qa", "perf", "product", "staff")
ROLE_LABELS = {"dev": "Dev", "qa": "QA", "perf": "Performance", "product": "Product", "staff": "Staff"}

# What a bot may do, and what it may not, shown to the user before setup.
ALLOWED = [
    "Read the tickets assigned to you in the active sprint.",
    "Move a ticket only along the stages its role owns (for example, dev takes a ticket from ready-for-dev to in-dev).",
    "Comment on tickets, and flag a ticket for a human when it needs a decision.",
    "Run the project's tests and its local app, in its own branch worktree.",
    "Log in and out of the local app with its own bot account, on this machine only.",
    "Commit to the agent branch in the internal repo.",
]
NOT_ALLOWED = [
    "Open GitHub pull requests or push to any remote. Only the publish step does, after you confirm.",
    "Clear a flag. Only you can clear it, after you have answered the question.",
    "Send anything to Jira directly. Changes go through the queue, and you confirm them.",
    "Touch tickets assigned to someone else, or repos that are not linked.",
    "Deploy to production, merge without both approvals, or skip the test run.",
    "Create accounts, change passwords, or read the credentials file.",
]

_ALPHABET = string.ascii_letters + string.digits


def credentials_path() -> Path:
    return Path(os.environ.get("WF_BOT_CREDENTIALS", str(Path.home() / ".config" / "wf" / "bots" / "credentials.env")))


def generate_password(length: int = 24) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def account_email(role: str, domain: str) -> str:
    return f"{role}@{domain}"


def write_credentials(path: Path, accounts: dict[str, dict[str, str]]) -> list[str]:
    """Write one `<ROLE>_EMAIL` and `<ROLE>_PASSWORD` pair per bot. Existing entries are kept.

    Returns the roles that were newly written. Values are never returned or printed.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = path.read_text().splitlines() if path.exists() else []
    have = {line.split("=", 1)[0] for line in existing if "=" in line}
    new_lines, written = [], []
    for role, acct in accounts.items():
        key = role.upper()
        if f"{key}_PASSWORD" in have:
            continue
        new_lines += [f"{key}_EMAIL={acct['email']}", f"{key}_PASSWORD={generate_password()}"]
        written.append(role)
    if new_lines:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write("\n".join(new_lines) + "\n")
    os.chmod(path, 0o600)
    return written


def setup_interactive(
    domain: str, *, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
    path: Path | None = None,
) -> dict | None:
    """Ask whether to create bot credentials, show the limits, and create them if the user agrees.

    Returns the bots config to save, or None if the user said no.
    """
    say("")
    say("Bot accounts")
    say("  The workflow can give each agent its own login to your app. Those logins exist only")
    say("  on the dev environment, and each one is limited to what its role needs.")
    say("")
    say("  Bots may:")
    for line in ALLOWED:
        say(f"    + {line}")
    say("  Bots may not:")
    for line in NOT_ALLOWED:
        say(f"    - {line}")
    say("")
    answer = ask("Create bot credentials now? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        say("Skipped. Run `wf bots setup` any time to create them.")
        return None
    accounts = {r: {"email": account_email(r, domain)} for r in ROLES}
    written = write_credentials(path or credentials_path(), accounts)
    say(f"Credentials written to {path or credentials_path()} (owner-only).")
    say(f"New accounts: {', '.join(written) if written else 'none; the file already had them'}.")
    say("Passwords are not shown here. Read the file yourself when you need one.")
    return {"enabled": True, "domain": domain, "roles": list(ROLES),
            "credentials_file": str(path or credentials_path())}


class FileBotAccounts:
    """BotAccounts over a credentials file outside the repos."""

    roles = ROLES
    allowed = ALLOWED
    not_allowed = NOT_ALLOWED

    def credentials_path(self) -> Path:
        return credentials_path()

    def account_email(self, role: str, domain: str) -> str:
        return account_email(role, domain)

    def write_credentials(self, path: Path, accounts: dict[str, dict[str, str]]) -> list[str]:
        return write_credentials(path, accounts)

    def setup_interactive(self, domain: str) -> dict | None:
        return setup_interactive(domain)
