"""Adapter for a local model served by Ollama (`POST /api/chat`)."""

from __future__ import annotations

import time

from wf.model import Message, ModelRequest, ModelResponse, Transport
from wf.model.transport import post_json


class OllamaModel:
    name = "ollama"

    def __init__(self, cfg, env: dict[str, str], transport: Transport | None = None):
        self.model = cfg.model
        self.base_url = cfg.base_url.rstrip("/")
        self._post = transport or post_json

    def complete(self, request: ModelRequest) -> ModelResponse:
        messages = [{"role": "system", "content": request.system}] if request.system else []
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": request.max_tokens},
        }
        started = time.monotonic()
        data = self._post(f"{self.base_url}/api/chat", {"content-type": "application/json"}, body)
        elapsed = int((time.monotonic() - started) * 1000)
        return ModelResponse(
            text=data.get("message", {}).get("content", ""),
            model=data.get("model", self.model),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            duration_ms=elapsed,
        )
