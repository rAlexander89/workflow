"""The model port: how the workflow talks to a language model.

The engine depends only on `ModelPort` and the plain dataclasses in `wf.domain.execution.model`. Adapters
implement the port for one provider each. Nothing outside the adapters imports a provider SDK, so swapping a
provider is a config change, not a code change.
"""

from __future__ import annotations

from typing import Callable, Protocol

from wf.domain.execution.model import ModelRequest, ModelResponse


class ModelPort(Protocol):
    name: str

    def complete(self, request: ModelRequest) -> ModelResponse: ...


# A transport posts JSON and returns the parsed JSON body. Injected so tests run offline.
Transport = Callable[[str, dict[str, str], dict], dict]
