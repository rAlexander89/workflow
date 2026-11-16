"""Onboarding: find the git repos a user works on, so they can pick which to link.

Only git is used. Each repo's `origin` remote gives the org/repo the work is pushed to.
The user's commits are counted by their git identity (`git config user.email`), so the
suggestions reflect what they actually work on. Nothing is sent anywhere.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoCandidate:
    path: str
    name: str  # the directory name, lowercased; the default link name
    remote: str  # "owner/name" from origin, or "" if there is no origin
    last_commit: int  # unix time of the newest commit, 0 if none
    my_commits: int  # commits made with the user's git email
    remote_url: str = ""  # the raw origin URL, so the host can be told apart (github.com or GitLab)


def _git(path: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def parse_remote(url: str) -> str:
    """Turn a git remote URL into "owner/name". Returns "" when it can't be read."""
    url = url.strip()
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def user_email() -> str:
    out = _git(Path.home(), "config", "--global", "user.email")
    return out or ""


def find_repos(roots: list[Path], max_depth: int = 3) -> list[Path]:
    """Git repositories under the given folders, up to max_depth levels down."""
    found: list[Path] = []
    seen: set[Path] = set()

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth or not folder.is_dir():
            return
        if (folder / ".git").exists():
            resolved = folder.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(folder)
            return  # do not look inside a repo for more repos
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for child in children:
            walk(child, depth + 1)

    for root in roots:
        walk(Path(root).expanduser(), 0)
    return found


def describe(path: Path, email: str) -> RepoCandidate:
    remote = _git(path, "remote", "get-url", "origin") or ""
    last = _git(path, "log", "-1", "--format=%ct")
    mine = _git(path, "rev-list", "--count", f"--author={email}", "HEAD") if email else None
    return RepoCandidate(
        path=str(path.resolve()),
        name=path.name.lower(),
        remote=parse_remote(remote),
        last_commit=int(last) if last and last.isdigit() else 0,
        my_commits=int(mine) if mine and mine.isdigit() else 0,
        remote_url=remote,
    )


def suggest(roots: list[Path], email: str | None = None, max_depth: int = 3) -> list[RepoCandidate]:
    """Repos under `roots`, most-worked-on first (your commits, then recency)."""
    email = user_email() if email is None else email
    candidates = [describe(p, email) for p in find_repos(roots, max_depth)]
    return sorted(candidates, key=lambda c: (c.my_commits, c.last_commit), reverse=True)
