"""The model port, under its old import path.

The vocabulary is `wf.domain.execution.model`, the port is `wf.application.ports.model`, and the provider
adapters and registry are in `wf.adapters.model`.
"""

from wf.adapters.model import _registry, build_model, known_providers  # noqa: F401
from wf.application.ports.model import ModelPort, Transport  # noqa: F401
from wf.domain.execution.model import (  # noqa: F401
    Message, ModelError, ModelRequest, ModelResponse, ProviderNotAllowed,
)
