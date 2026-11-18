"""Provider clients. For the shell they only verify that a link resolves. Nothing here
writes to Jira or to any repo.

Internal repos are local git repositories. A repo check confirms the path is a git
repository on this machine.

Jira uses its REST API with the credentials in the environment:
    JIRA_BASE_URL   e.g. https://yourcompany.atlassian.net
    JIRA_EMAIL      the account the token belongs to
    JIRA_API_TOKEN  an API token for that account
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from wf.application.ports.tracker import TrackerError
from wf.domain.workspace.config import BoardLink, RepoLink


@dataclass(frozen=True)
class Check:
    ok: bool
    detail: str


Runner = Callable[..., subprocess.CompletedProcess]


def check_repo(link: RepoLink, runner: Runner = subprocess.run) -> Check:
    """Confirm the linked path is a git repository on this machine."""
    proc = runner(["git", "-C", link.path, "rev-parse", "--is-bare-repository"],
                  capture_output=True, text=True)
    if proc.returncode != 0:
        return Check(False, f"not a git repository: {link.path}")
    kind = "bare" if proc.stdout.strip() == "true" else "worktree"
    return Check(True, f"{kind} repository at {link.path}")


class JiraClient:
    def __init__(self, env: dict[str, str] | None = None, opener=urllib.request.urlopen):
        self._env = env if env is not None else dict(os.environ)
        self._open = opener

    def configured(self) -> bool:
        return all(self._env.get(k) for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))

    def _get(self, path: str) -> dict:
        if not self.configured():
            raise JiraError("JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN must be set")
        base = self._env["JIRA_BASE_URL"].rstrip("/")
        url = f"{base}{path}"
        token = base64.b64encode(f"{self._env['JIRA_EMAIL']}:{self._env['JIRA_API_TOKEN']}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
        try:
            with self._open(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise JiraError(f"HTTP {e.code} from {url}") from e
        except urllib.error.URLError as e:
            raise JiraError(f"could not reach Jira: {e.reason}") from e

    def check_board(self, link: BoardLink) -> Check:
        try:
            body = self._get(f"/rest/agile/1.0/board/{link.ref}")
        except JiraError as e:
            return Check(False, str(e))
        return Check(True, body.get("name", "(unnamed board)"))

    def search_keys(self, jql: str, page_size: int = 100) -> list[str]:
        """Issue keys matching a JQL query, via /rest/api/3/search/jql (paged by nextPageToken)."""
        keys: list[str] = []
        token: str | None = None
        while True:
            q = {"jql": jql, "maxResults": str(page_size), "fields": "key"}
            if token:
                q["nextPageToken"] = token
            body = self._get("/rest/api/3/search/jql?" + urllib.parse.urlencode(q))
            keys.extend(issue["key"] for issue in body.get("issues", []))
            token = body.get("nextPageToken")
            if not token:
                return keys

    def get_issue_changelog(self, issue_key: str) -> dict:
        """The issue with its full changelog (status history), straight from the REST API."""
        return self._get(f"/rest/api/3/issue/{issue_key}?expand=changelog&fields=status,labels")


class JiraError(TrackerError):
    pass
