"""Where the code lives: GitHub or GitLab, behind one interface.

Each linked repo names its forge in the config ("github" or "gitlab"). A PR or merge request
link picks its forge from its host. Calls go through the `gh` CLI for GitHub and the `glab`
CLI for GitLab, so both use the logins already set up on this machine. Nothing here is
forge-specific outside this module.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import Protocol

from wf.application.ports.forge import Forge  # noqa: F401
from wf.domain.delivery.checks import pairs_state as checks_state  # noqa: F401

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


@dataclass
class GitHub:
    runner: Runner = _run
    name: str = "github"

    def pr_status(self, url: str) -> dict:
        proc = self.runner(["gh", "pr", "view", url, "--json",
                            "state,mergedAt,mergeStateStatus,statusCheckRollup,headRefOid,commits,mergeCommit"], Path("."))
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or "gh failed").strip()[-200:]}
        data = json.loads(proc.stdout or "{}")
        rollup = data.get("statusCheckRollup") or []
        return {
            "ok": True, "state": data.get("state", ""), "merged": bool(data.get("mergedAt")),
            "checks": checks_state([(c.get("status", ""), c.get("conclusion", "")) for c in rollup]),
            "merge_state": data.get("mergeStateStatus", ""), "head": data.get("headRefOid", ""),
            "commit_subjects": [c.get("messageHeadline", "") for c in data.get("commits") or []],
            "merge_sha": (data.get("mergeCommit") or {}).get("oid", ""),
        }

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        proc = self.runner(["gh", "pr", "create", "--repo", repo, "--base", "main", "--head", branch,
                            "--title", title, "--body", body], Path("."))
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-300:])
        return proc.stdout.strip().splitlines()[-1]

    def merge_pr(self, url: str) -> bool:
        return self.runner(["gh", "pr", "merge", url, "--merge"], Path(".")).returncode == 0

    def runs(self, repo: str, branch: str) -> list[dict]:
        proc = self.runner(["gh", "run", "list", "--repo", repo, "--branch", branch, "--limit", "50",
                            "--json", "databaseId,headSha,name,status,conclusion,url"], Path("."))
        if proc.returncode != 0:
            return []
        out = []
        for r in json.loads(proc.stdout or "[]"):
            out.append({"id": str(r.get("databaseId", "")), "sha": r.get("headSha", ""), "name": r.get("name", ""),
                        "status": (r.get("status") or "").lower(),
                        "conclusion": (r.get("conclusion") or "").lower(), "url": r.get("url", "")})
        return out

    def jobs(self, repo: str, run_id: str) -> list[dict]:
        proc = self.runner(["gh", "run", "view", run_id, "--repo", repo, "--json", "jobs"], Path("."))
        return [{"id": str(j.get("databaseId", "")), "name": j.get("name", ""),
                 "status": (j.get("status") or "").lower(), "started": j.get("startedAt") or "",
                 "conclusion": (j.get("conclusion") or "").lower(), "url": j.get("url", "")}
                for j in json.loads(proc.stdout or "{}").get("jobs", [])]

    def job_messages(self, repo: str, job_id: str) -> list[str]:
        proc = self.runner(["gh", "api", f"repos/{repo}/check-runs/{job_id}/annotations"], Path("."))
        return [a.get("message", "") for a in json.loads(proc.stdout or "[]")]

    def dispatch_prod(self, repo: str, sha: str) -> bool:
        proc = self.runner(["gh", "workflow", "run", "deploy-prod.yml", "--repo", repo, "-f", f"commit={sha}"], Path("."))
        return proc.returncode == 0


@dataclass
class GitLab:
    host: str = "gitlab.local"
    runner: Runner = _run
    name: str = "gitlab"

    def _api(self, path: str, method: str = "GET", fields: dict | None = None) -> dict | list | None:
        argv = ["glab", "api", "--hostname", self.host, path]
        if method != "GET":
            argv += ["-X", method]
        for k, v in (fields or {}).items():
            argv += ["-f", f"{k}={v}"]
        proc = self.runner(argv, Path("."))
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-300:] or f"glab api {path} failed")
        return json.loads(proc.stdout or "null")

    @staticmethod
    def _project(repo: str) -> str:
        return urllib.parse.quote(repo, safe="")

    @staticmethod
    def _mr_parts(url: str) -> tuple[str, str]:
        # https://host/group/project/-/merge_requests/12 -> ("group/project", "12")
        path = urllib.parse.urlparse(url).path.strip("/")
        project, _, rest = path.partition("/-/merge_requests/")
        return project, rest.split("/")[0]

    def pr_status(self, url: str) -> dict:
        try:
            project, iid = self._mr_parts(url)
            mr = self._api(f"/projects/{self._project(project)}/merge_requests/{iid}") or {}
            commits = self._api(f"/projects/{self._project(project)}/merge_requests/{iid}/commits") or []
            pipelines = mr.get("head_pipeline") or {}
            jobs = self._api(f"/projects/{self._project(project)}/pipelines/{pipelines.get('id')}/jobs") if pipelines.get("id") else []
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        state = {"opened": "OPEN", "merged": "MERGED", "closed": "CLOSED"}.get(mr.get("state", ""), mr.get("state", "").upper())
        return {
            "ok": True, "state": state, "merged": mr.get("state") == "merged",
            "checks": checks_state([(j.get("status", ""), j.get("status", "")) for j in (jobs or [])]),
            "merge_state": mr.get("detailed_merge_status", ""), "head": mr.get("sha", ""),
            "commit_subjects": [c.get("title", "") for c in commits],
            "merge_sha": mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or "",
        }

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        mr = self._api(f"/projects/{self._project(repo)}/merge_requests", "POST",
                       {"source_branch": branch, "target_branch": "main", "title": title, "description": body})
        return (mr or {}).get("web_url", "")

    def merge_pr(self, url: str) -> bool:
        try:
            project, iid = self._mr_parts(url)
            self._api(f"/projects/{self._project(project)}/merge_requests/{iid}/merge", "PUT")
            return True
        except RuntimeError:
            return False

    def runs(self, repo: str, branch: str) -> list[dict]:
        try:
            pipes = self._api(f"/projects/{self._project(repo)}/pipelines?ref={branch}&per_page=50") or []
        except RuntimeError:
            return []
        return [{"id": str(p.get("id", "")), "sha": p.get("sha", ""), "name": p.get("name") or "pipeline",
                 "status": "completed" if p.get("status") in ("success", "failed", "canceled", "skipped") else "in_progress",
                 "conclusion": {"success": "success", "failed": "failure", "canceled": "cancelled"}.get(p.get("status", ""), ""),
                 "url": p.get("web_url", "")} for p in pipes]

    def jobs(self, repo: str, run_id: str) -> list[dict]:
        try:
            jobs = self._api(f"/projects/{self._project(repo)}/pipelines/{run_id}/jobs") or []
        except RuntimeError:
            return []
        return [{"id": str(j.get("id", "")), "name": j.get("name", ""),
                 "conclusion": {"success": "success", "failed": "failure"}.get(j.get("status", ""), ""),
                 "url": j.get("web_url", "")} for j in jobs]

    def dispatch_prod(self, repo: str, sha: str) -> bool:
        try:
            self._api(f"/projects/{self._project(repo)}/pipeline", "POST",
                      {"ref": "main", "variables[0][key]": "COMMIT", "variables[0][value]": sha})
            return True
        except RuntimeError:
            return False

    def job_messages(self, repo: str, job_id: str) -> list[str]:
        try:
            j = self._api(f"/projects/{self._project(repo)}/jobs/{job_id}") or {}
        except RuntimeError:
            return []
        return [j.get("failure_reason", "")] if j.get("failure_reason") else []


def for_url(url: str, *, runner: Runner = _run) -> Forge:
    """Pick the forge from a PR or merge request link."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("", "github.com"):
        return GitHub(runner=runner)
    return GitLab(host=host, runner=runner)


def for_repo(forge_name: str, *, host: str = "gitlab.local") -> Forge:
    return GitHub() if forge_name == "github" else GitLab(host=host)
