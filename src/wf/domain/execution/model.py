"""The language-model vocabulary: requests, responses, and the errors a call can raise."""

from __future__ import annotations

from dataclasses import dataclass


class ModelError(RuntimeError):
    """A model call failed. The message never contains credentials."""


class ProviderNotAllowed(ModelError):
    """The configured provider is not enabled for this workflow."""


@dataclass(frozen=True)
class Message:
    role: str  # "user" or "assistant"
    content: str


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: tuple[Message, ...]
    max_tokens: int = 1024


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int | None = None
