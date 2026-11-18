"""Role prompts, read from the prompts folder."""

from __future__ import annotations

from pathlib import Path


class FilePromptLibrary:
    def __init__(self, directory: Path):
        self._dir = Path(directory)

    def role_prompt(self, role: str) -> str:
        path = self._dir / f"{role}.md"
        if not path.exists():
            raise FileNotFoundError(f"no prompt for role {role!r} at {path}")
        return path.read_text()
