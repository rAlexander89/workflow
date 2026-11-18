"""Setup and diagnostic ports: bot accounts, the project's local app, machine checks, and finding repos."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class BotAccounts(Protocol):
    roles: tuple[str, ...]
    allowed: list[str]  # what a bot may do, shown before setup
    not_allowed: list[str]

    def credentials_path(self) -> Path: ...

    def account_email(self, role: str, domain: str) -> str: ...

    def write_credentials(self, path: Path, accounts: dict[str, dict[str, str]]) -> list[str]:
        """Generate and store a password for each new account. Returns the roles that got one."""
        ...

    def setup_interactive(self, domain: str) -> dict | None:
        """Ask the user, then set the accounts up. Returns the bots config to save, or None if declined."""
        ...


class AppEnvironment(Protocol):
    """Start and stop the project's app on this machine."""

    def status(self, state_dir: Path, name: str) -> dict | None: ...

    def up(self, name: str, *, repo: Path, app_cmd: str, state_dir: Path, app_url: str = "") -> dict: ...

    def down(self, name: str, *, state_dir: Path) -> bool: ...


class MachineChecks(Protocol):
    """`wf doctor`: can this machine run the Claude Code flow."""

    def run_checks(self, *, claude_code_access: bool) -> list: ...

    def format_checks(self, checks: list) -> str: ...


class RepoFinder(Protocol):
    def suggest(self, roots: list[Path]) -> list:
        """The git repos under these folders the user works on, most worked on first."""
        ...
