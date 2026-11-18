"""Workspace config on disk: a JSON file of identifiers. Credentials come from the environment and are
never written here. The types and rules are in `wf.domain.workspace.config`."""

from __future__ import annotations

import json
import os
from pathlib import Path

from wf.domain.workspace.config import (  # noqa: F401
    BOARD_PROVIDERS, REPO_NAME_RE, AgentsConfig, BoardLink, BotsConfig, Config, ConfigError, ModelConfig, RepoLink,
)


def default_path() -> Path:
    return Path(os.environ.get("WF_CONFIG", "workflow.json"))


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
