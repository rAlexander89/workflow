"""A ticket's key, as the tracker writes it: PROJECT-123."""

from __future__ import annotations

import re

_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


class InvalidTicketKey(ValueError):
    pass


class TicketKey(str):
    """A validated ticket key. It is a string, so it works anywhere a key is expected."""

    def __new__(cls, value: str) -> "TicketKey":
        if not isinstance(value, str) or not _KEY.fullmatch(value):
            raise InvalidTicketKey(f"{value!r} is not a ticket key like ABC-12")
        return super().__new__(cls, value)

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return isinstance(value, str) and bool(_KEY.fullmatch(value))

    @property
    def project(self) -> str:
        return self.split("-", 1)[0]
