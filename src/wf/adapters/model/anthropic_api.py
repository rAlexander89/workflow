"""Adapter for the Claude API (`POST /v1/messages`), billed per token with an API key.

The key is read once from the environment variable named in config. It is sent
only in the `x-api-key` header and is never logged or put in an error message.
"""

from __future__ import annotations

import time

from wf.application.ports.model import Transport
from wf.domain.execution.model import ModelError, ModelRequest, ModelResponse
from wf.adapters.model.transport import post_json

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicAPIModel:
    name = "anthropic-api"

    def __init__(self, cfg, env: dict[str, str], transport: Transport | None = None):
        self.model = cfg.model
        key = env.get(cfg.api_key_env, "")
        if not key:
            raise ModelError(f"environment variable {cfg.api_key_env} is not set")
        self._key = key
        self._post = transport or post_json

    def complete(self, request: ModelRequest) -> ModelResponse:
        body = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": self._key,
            "anthropic-version": API_VERSION,
        }
        started = time.monotonic()
        data = self._post(API_URL, headers, body)
        elapsed = int((time.monotonic() - started) * 1000)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return ModelResponse(
            text=text,
            model=data.get("model", self.model),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            duration_ms=elapsed,
        )

