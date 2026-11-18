"""Test data: Jira sync payloads, git repos, and a container wired for a test."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from wf.adapters.config import workspace as cfgmod
from wf.bootstrap import Container, Overrides

NOW = 1000.0


def clock() -> float:
    return NOW


STATUSES = [
    {"name": "Backlog", "category": "new"},
    {"name": "Ready for dev", "category": "new"},
    {"name": "Dev in progress", "category": "indeterminate"},
    {"name": "PR open", "category": "indeterminate"},
    {"name": "Deployed to dev", "category": "done"},
]
PROJECT = {"key": "ABC", "name": "Example", "statuses": STATUSES}
PROJECT_WITH_MERGED = {**PROJECT, "statuses": PROJECT["statuses"] + [{"name": "Merged to main", "category": "indeterminate"}]}


def sprint(sid, state, name=None):
    return {"id": sid, "board_id": "7", "name": name or f"Sprint {sid}", "state": state}


def ticket(key, status="Ready for dev", sprint_id="101", **extra):
    t = {"key": key, "project_key": "ABC", "jira_id": key.split("-")[1] + "00",
         "summary": f"Work {key}", "status": status, "status_category": "new",
         "labels": [], "sprint_id": sprint_id, "assignee_id": "me", "updated": 1}
    t.update(extra)
    return t


def backlog_ticket(key, status="Backlog", **extra):
    return ticket(key, status=status, sprint_id=None, **extra)


def payload(kind="active", **parts):
    data = {"kind": kind, "projects": [PROJECT], "sprints": [sprint("101", "active")],
            "tickets": [], "comments": []}
    data.update(parts)
    return data


# --- git ----------------------------------------------------------------------------------------

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(["init", "-q", "-b", "main"], path)
    (path / "app.txt").write_text("v1\n")
    git(["add", "-A"], path)
    git(["commit", "-q", "-m", "init"], path)
    return path


# --- a container for a test ---------------------------------------------------------------------

def make_container(tmp_path: Path, *, repo: Path | None = None, overrides: Overrides | None = None,
                   test_cmd: str = "true", access: bool = True, remote: str = "acme/app", forge: str = "github",
                   regression_url: str = "", app_url: str = "", bots_domain: str = "", bots_credentials: str = "",
                   clock=clock, **agents) -> Container:
    """A container over a fresh database and a config that links `repo` (if given)."""
    cfg = cfgmod.Config()
    if repo is not None:
        cfg.add_repo("app", str(repo), remote=remote, forge=forge)
    cfg.agents.runs_dir = str(tmp_path / "runs")
    cfg.agents.test_cmd = test_cmd
    cfg.agents.regression_url = regression_url
    cfg.agents.app_url = app_url
    for name, value in agents.items():
        setattr(cfg.agents, name, value)
    cfg.model = cfgmod.ModelConfig(claude_code_access=access)
    if bots_domain:
        cfg.bots = cfgmod.BotsConfig(enabled=True, domain=bots_domain, credentials_file=bots_credentials)
    config_path = tmp_path / "workflow.json"
    cfgmod.save(cfg, config_path)
    return Container(tmp_path / "wf.sqlite", config_path, clock=clock, runner_secret="",
                     claude_bin=lambda: "/bin/echo", overrides=overrides)


def sync(conn, data):
    """Apply a sync payload to the board in `conn`, as the Jira sync does."""
    from wf.adapters.persistence.sqlite.board_sync import SqliteBoardSync
    return SqliteBoardSync(conn).apply(data, clock)


def ticket_row(conn, key):
    return conn.execute("select * from ticket where key = ?", (key,)).fetchone()


class BoardOver:
    """The board's use cases and queries over one connection to a database file, for tests that act on a
    database an app under test is also using."""

    def __init__(self, db):
        from wf.adapters.persistence.sqlite.board import SqliteBoardUnitOfWork
        from wf.adapters.persistence.sqlite.board_queries import SqliteBoardQueries
        from wf.adapters.persistence.sqlite.board_sync import SqliteBoardSync
        from wf.adapters.persistence.sqlite.schema import connect
        from wf.application.board.tickets import TicketCommands
        from wf.application.tracker.outbox import OutboxService
        from wf.bootstrap import build_event_bus
        self.conn = connect(db)
        uow = lambda: SqliteBoardUnitOfWork(self.conn)  # noqa: E731
        self.tickets = TicketCommands(uow, clock, build_event_bus())
        self.outbox = OutboxService(uow, clock, build_event_bus())
        self.queries = SqliteBoardQueries(self.conn)
        self.sync = SqliteBoardSync(self.conn)

    def apply(self, data):
        return self.sync.apply(data, clock)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()


def board_over(db) -> BoardOver:
    return BoardOver(db)


# --- seeding the board directly, for tests that start from a ticket already in some stage -------------

def set_stage(conn, key, stage):
    conn.execute("update ticket set stage = ? where key = ?", (stage, key))
    conn.commit()


def insert_ticket(conn, key, stage, sort_order=None):
    conn.execute("insert into ticket (key, project_key, jira_id, summary, assignee_id, jira_status, labels, "
                 "sprint_id, stage, flags, removed_at, synced_at, sort_order) values (?, 'ABC', ?, 'x', 'me', "
                 "'Ready for dev', '[]', '101', ?, '[]', null, 0, ?)", (key, key, stage, sort_order))
    conn.commit()
