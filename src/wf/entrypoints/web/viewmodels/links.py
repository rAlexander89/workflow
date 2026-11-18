"""The links page: the repos and Jira boards this workflow is linked to, and checking that they resolve."""

from __future__ import annotations

from dataclasses import dataclass, field

from wf.domain.workspace.config import ConfigError
from wf.entrypoints.web.viewmodels.base import ViewModel


@dataclass
class LinksPanelViewModel(ViewModel):
    """The lists of repos and boards. It is a page fragment: the commands below replace it in place."""
    template = "_links.html"
    repos: list = field(default_factory=list)
    boards: list = field(default_factory=list)
    error: str | None = None

    @classmethod
    def load(cls, container, error: str | None = None) -> "LinksPanelViewModel":
        c = container.config()
        return cls(repos=c.repos, boards=c.boards, error=error)

    # --- what a person can do ----------------------------------------------------------------

    @classmethod
    def add_repo(cls, container, name: str, path: str) -> "LinksPanelViewModel":
        return cls._change(container, lambda c: c.add_repo(name, path))

    @classmethod
    def remove_repo(cls, container, name: str) -> "LinksPanelViewModel":
        return cls._change(container, lambda c: c.remove_repo(name))

    @classmethod
    def add_board(cls, container, provider: str, ref: str, label: str) -> "LinksPanelViewModel":
        return cls._change(container, lambda c: c.add_board(provider, ref, label.strip()))

    @classmethod
    def remove_board(cls, container, provider: str, ref: str) -> "LinksPanelViewModel":
        return cls._change(container, lambda c: c.remove_board(provider, ref))

    @classmethod
    def _change(cls, container, change) -> "LinksPanelViewModel":
        c = container.config()
        error = None
        try:
            change(c)
            container.save_config(c)
        except ConfigError as e:
            error = str(e)
        return cls(repos=c.repos, boards=c.boards, error=error)


@dataclass
class LinksViewModel(LinksPanelViewModel):
    template = "links.html"
    active = "links"


@dataclass(frozen=True)
class CheckRow:
    kind: str
    name: str
    ok: bool
    detail: str


@dataclass
class CheckViewModel(ViewModel):
    template = "_check.html"
    rows: list = field(default_factory=list)

    @classmethod
    def run(cls, container, issue_reader) -> "CheckViewModel":
        """Check every linked repo and board, and report each."""
        c = container.config()
        rows = [CheckRow("repo", r.name, *_ok_detail(container.check_repo(r))) for r in c.repos]
        rows += [CheckRow(f"board ({b.provider})", b.ref, *_ok_detail(issue_reader.check_board(b))) for b in c.boards]
        return cls(rows=rows)


def _ok_detail(result) -> tuple[bool, str]:
    return result.ok, result.detail
