"""A git commit, identified by its full hash."""

from __future__ import annotations

import re

_SHA = re.compile(r"[0-9a-f]{7,64}")


class InvalidCommit(ValueError):
    pass


class CommitSha(str):
    """A commit hash. An approval covers exactly one of these."""

    def __new__(cls, value: str) -> "CommitSha":
        if not isinstance(value, str) or not _SHA.fullmatch(value.lower()):
            raise InvalidCommit(f"{value!r} is not a commit hash")
        return super().__new__(cls, value)

    @property
    def short(self) -> str:
        return self[:8]
