"""Workspace config: the internal repos and the Jira boards this workflow is linked to.

The file holds identifiers only. Credentials come from the environment and are
never written here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
BOARD_PROVIDERS = ("jira",)


class ConfigError(ValueError):
    pass


def default_path() -> Path:
    return Path(os.environ.get("WF_CONFIG", "workflow.json"))


@dataclass(frozen=True)
class RepoLink:
    name: str  # the internal repo's name, e.g. "api"
    path: str  # where the repo lives on this machine
    remote: str = ""  # the org/repo it pushes to later, e.g. "acme/api"; empty if unknown


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
class Config:
    repos: list[RepoLink] = field(default_factory=list)
    boards: list[BoardLink] = field(default_factory=list)
    model: ModelConfig = field(default_factory=ModelConfig)

    # --- repos -------------------------------------------------------------

    def add_repo(self, name: str, path: str, remote: str = "") -> bool:
        """Returns False if a repo with this name is already linked."""
        name = name.strip()
        path = path.strip()
        if not REPO_NAME_RE.match(name):
            raise ConfigError(f"repo name must be lowercase letters, digits, '.', '_' or '-', got {name!r}")
        if not path:
            raise ConfigError("repo path must not be empty")
        if any(r.name == name for r in self.repos):
            return False
        self.repos.append(RepoLink(name, path, remote.strip()))
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
            "repos": [{"name": r.name, "path": r.path, "remote": r.remote} for r in self.repos],
            "boards": [{"provider": b.provider, "ref": b.ref, "label": b.label} for b in self.boards],
            "model": asdict(self.model),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = cls()
        for r in data.get("repos", []):
            cfg.add_repo(r["name"], r["path"], r.get("remote", ""))
        for b in data.get("boards", []):
            cfg.add_board(b["provider"], b["ref"], b.get("label", ""))
        if "model" in data:
            cfg.model = ModelConfig(**data["model"])
        return cfg


def load(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        return Config.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError) as e:
        raise ConfigError(f"{path} is not a valid workflow config: {e}") from e


def save(cfg: Config, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n")
    tmp.replace(path)
