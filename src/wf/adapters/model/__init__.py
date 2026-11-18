"""Model adapters, one per provider, and the registry that builds the one the config names.

- `ollama`: a local model served by Ollama. No cloud terms apply.
- `anthropic-api`: the Claude API, billed per token, authenticated with an API key.
- `claude-code`: the Claude Code CLI under this host's own login. Only where access is provisioned.
"""

from __future__ import annotations

from typing import Callable

from wf.application.ports.model import ModelPort, Transport
from wf.domain.execution.model import ProviderNotAllowed


def _registry() -> dict[str, Callable[..., ModelPort]]:
    # Imported here so the port module has no provider imports at load time.
    from wf.adapters.model.anthropic_api import AnthropicAPIModel
    from wf.adapters.model.claude_code import ClaudeCodeModel
    from wf.adapters.model.ollama import OllamaModel

    return {"ollama": OllamaModel, "anthropic-api": AnthropicAPIModel, "claude-code": ClaudeCodeModel}


def known_providers() -> list[str]:
    return sorted(_registry())


def build_model(cfg, env: dict[str, str], transport: Transport | None = None) -> ModelPort:
    """Create the adapter named in `cfg` (a `wf.config.ModelConfig`)."""
    registry = _registry()
    if cfg.provider not in registry:
        raise ProviderNotAllowed(
            f"unknown model provider {cfg.provider!r}; choose one of {known_providers()}"
        )
    return registry[cfg.provider](cfg, env=env, transport=transport)
