"""What the harness is configured with, as the use cases see it. Built from the workspace config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wf.domain.board.errors import BoardError


@dataclass(frozen=True)
class RepoSettings:
    name: str
    path: str
    remote: str = ""  # org/repo it pushes to
    forge: str = "github"  # github, gitlab, or local (plain git, no PR host)
    host: str = ""  # the GitLab host


@dataclass(frozen=True)
class Settings:
    repos: tuple[RepoSettings, ...] = ()
    runs_dir: Path = Path("wf-runs")
    test_cmd: str = ""
    app_cmd: str = ""
    app_url: str = ""
    regression_url: str = ""
    model: str = ""
    claude_code_access: bool = False
    auto_run: bool = False
    bot_domain: str = ""  # empty when bots are off
    bot_credentials: str = ""

    def repo_path(self, name: str | None = None) -> Path:
        repos = {r.name: r for r in self.repos}
        if not repos:
            raise BoardError("no internal repo is linked; add one on the Links page")
        if name is None:
            if len(repos) > 1:
                raise BoardError("several repos are linked; this page supports one")
            name = next(iter(repos))
        return Path(repos[name].path)

    def link(self) -> RepoSettings | None:
        return self.repos[0] if self.repos else None

    def repo_slug(self) -> str:
        if not self.repos or not self.repos[0].remote:
            raise BoardError("the linked repo has no GitHub remote (org/repo) set")
        return self.repos[0].remote
