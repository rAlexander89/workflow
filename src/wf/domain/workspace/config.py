"""Workspace config: the internal repos and the Jira boards this workflow is linked to.

The file holds identifiers only. Credentials come from the environment and are
never written here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
BOARD_PROVIDERS = ("jira",)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RepoLink:
    name: str  # the internal repo's name, e.g. "api"
    path: str  # where the repo lives on this machine
    remote: str = ""  # the org/repo it pushes to later, e.g. "acme/api"; empty if unknown
    forge: str = "github"  # "github" or "gitlab"
    host: str = ""  # the GitLab host, e.g. "git.example.com"; unused for GitHub


@dataclass(frozen=True)
class BoardLink:
    provider: str  # "jira"
    ref: str  # the Jira agile board id, e.g. "12"
    label: str = ""


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "ollama"  # "ollama", "anthropic-api", or "claude-code" (see wf.model)
    model: str = "qwen3.5:9b"
    base_url: str = "http://127.0.0.1:11434"  # Ollama only
    api_key_env: str = "ANTHROPIC_API_KEY"  # name of the env var holding the key; the key itself is never stored here
    # Off by default. Only set on a machine whose operator has provisioned Claude Code
    # for this workflow. Gates both the claude-code provider and `wf run-claude`.
    claude_code_access: bool = False


@dataclass
class AgentsConfig:
    runs_dir: str = "~/wf-runs"  # worktrees and run output live here, outside the repos
    test_cmd: str = "make test"  # runs in each ticket's worktree; a merge needs it to pass
    model: str = "claude-sonnet-5-5"  # Claude model for agent runs
    app_cmd: str = ""  # starts the project's app; "{port}" is the port the agent gets (empty = no app)
    app_url: str = ""  # a local app already running (e.g. http://127.0.0.1:3002); used instead of starting one
    regression_url: str = ""  # where the final regression runs, e.g. https://dev.example.com
    auto_run: bool = False  # start the next pass by itself when a ticket is ready (after a flag clears, etc.)


@dataclass
class BotsConfig:
    enabled: bool = False  # set by `wf bots setup` once the user agrees
    domain: str = ""  # the bot accounts are <role>@<domain>
    roles: list = field(default_factory=list)
    credentials_file: str = ""  # outside the repos; holds the passwords, owner-only


@dataclass
class Config:
    repos: list[RepoLink] = field(default_factory=list)
    boards: list[BoardLink] = field(default_factory=list)
    model: ModelConfig = field(default_factory=ModelConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    bots: BotsConfig = field(default_factory=BotsConfig)

    # --- repos -------------------------------------------------------------

    def add_repo(self, name: str, path: str, remote: str = "", forge: str = "github", host: str = "") -> bool:
        """Returns False if a repo with this name is already linked.

        forge: "github", "gitlab" (hosted or on this network, see host), or "local" (plain git, no PR host).
        """
        if forge not in ("github", "gitlab", "local"):
            raise ConfigError(f"forge must be github, gitlab, or local, got {forge!r}")
        name = name.strip()
        path = path.strip()
        if not REPO_NAME_RE.match(name):
            raise ConfigError(f"repo name must be lowercase letters, digits, '.', '_' or '-', got {name!r}")
        if not path:
            raise ConfigError("repo path must not be empty")
        if any(r.name == name for r in self.repos):
            return False
        self.repos.append(RepoLink(name, path, remote.strip(), forge, host.strip()))
        return True

    def remove_repo(self, name: str) -> bool:
        before = len(self.repos)
        self.repos = [r for r in self.repos if r.name != name.strip()]
        return len(self.repos) != before

    # --- boards ------------------------------------------------------------

    def add_board(self, provider: str, ref: str, label: str = "") -> bool:
        """Returns False if the same board was already linked."""
        if provider not in BOARD_PROVIDERS:
            raise ConfigError(f"board provider must be one of {BOARD_PROVIDERS}, got {provider!r}")
        ref = ref.strip()
        if not ref.isdigit():
            raise ConfigError(f"jira board ref must be a numeric board id, got {ref!r}")
        if any(b.provider == provider and b.ref == ref for b in self.boards):
            return False
        self.boards.append(BoardLink(provider, ref, label))
        return True

    def remove_board(self, provider: str, ref: str) -> bool:
        before = len(self.boards)
        self.boards = [b for b in self.boards if not (b.provider == provider and b.ref == ref.strip())]
        return len(self.boards) != before

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "repos": [{"name": r.name, "path": r.path, "remote": r.remote, "forge": r.forge, "host": r.host}
                      for r in self.repos],
            "boards": [{"provider": b.provider, "ref": b.ref, "label": b.label} for b in self.boards],
            "model": asdict(self.model),
            "agents": asdict(self.agents),
            "bots": asdict(self.bots),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = cls()
        for r in data.get("repos", []):
            cfg.add_repo(r["name"], r["path"], r.get("remote", ""), forge=r.get("forge", "github"),
                         host=r.get("host", ""))
        for b in data.get("boards", []):
            cfg.add_board(b["provider"], b["ref"], b.get("label", ""))
        if "model" in data:
            cfg.model = ModelConfig(**data["model"])
        if "agents" in data:
            cfg.agents = AgentsConfig(**data["agents"])
        if "bots" in data:
            cfg.bots = BotsConfig(**data["bots"])
        return cfg
