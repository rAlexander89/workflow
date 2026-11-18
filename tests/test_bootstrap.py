"""The composition root builds every use case from a config and a database."""
import subprocess

import pytest

from tests.test_agents import GIT_ENV
from tests.test_board import PROJECT, payload, sync, ticket
from wf.adapters.config import workspace as cfgmod
from wf.application.settings import Settings
from wf.bootstrap import Container
from wf.domain.board.errors import BoardError


class Log:
    def __init__(self): self.lines = []
    def log(self, message): self.lines.append(message)


class Tasks:
    def __init__(self): self.started, self.running = [], False
    def start(self, label, work):
        if self.running:
            return False
        self.started.append(label)
        return True
    def busy(self): return self.running


@pytest.fixture
def container(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, env=GIT_ENV, check=True)
    cfg = cfgmod.Config()
    cfg.add_repo("app", str(repo), remote="acme/app")
    cfg.agents.runs_dir = str(tmp_path / "runs")
    cfg.agents.test_cmd = "true"
    cfg.agents.auto_run = True
    cfg.model = cfgmod.ModelConfig(claude_code_access=True)
    cfgmod.save(cfg, tmp_path / "workflow.json")
    log = Log()
    c = Container(tmp_path / "wf.sqlite", tmp_path / "workflow.json", clock=lambda: 1000.0, log=log,
                  runner_secret="", claude_bin=lambda: "/bin/echo")
    c.test_log = log
    return c


def test_the_settings_come_from_the_config(container, tmp_path):
    s = container.settings()
    assert isinstance(s, Settings) and s.claude_code_access and s.auto_run and s.test_cmd == "true"
    assert s.repo_path().name == "repo" and s.repo_slug() == "acme/app" and s.runs_dir == (tmp_path / "runs").resolve()
    assert s.bot_domain == ""


def test_settings_without_a_repo_say_so(tmp_path):
    s = Container(tmp_path / "wf.sqlite", tmp_path / "none.json").settings()
    with pytest.raises(BoardError, match="no internal repo is linked"):
        s.repo_path()
    with pytest.raises(BoardError, match="no GitHub remote"):
        s.repo_slug()


def test_a_session_builds_every_use_case(container):
    s = container.settings()
    with container.session() as session:
        sync(session.conn, payload(projects=[PROJECT], tickets=[ticket("ABC-1", status="Ready for dev")]))
        for build in (session.pipeline, session.agents, session.passes, session.run_ticket, session.merge,
                      session.merge_bot, session.publish, session.deploy, session.jira, session.observer):
            assert build(s) is not None
        assert [i["key"] for i in session.pickup().plan().to_dict()["pick"]] == ["ABC-1"]
        assert [a["key"] for a in session.passes(s).plan(dry_run=True)] == ["ABC-1"]  # the dev would take it
        assert session.queries.ticket_detail("ABC-1")["ticket"]["key"] == "ABC-1"


def test_a_cycle_does_nothing_while_paused_and_keeps_a_waiting_jira_change(container):
    s = container.settings()
    tasks = Tasks()
    with container.session() as session:
        session.pause.pause("test", 1)
        assert session.cycle(s, tasks).run_once(jira_changed=True) is True
        assert tasks.started == []
        session.pause.resume(2)
        assert session.cycle(s, tasks).run_once(jira_changed=True) is False
        assert tasks.started[0] == "jira sync"
        tasks.running = True
        assert session.cycle(s, tasks).run_once(jira_changed=True) is True  # busy: try again next time
