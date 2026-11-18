"""Running an agent: the briefing and environment rules, and the service with fake runner and workspace."""
from pathlib import Path

import pytest

from tests.fakes import MemoryAgentRuns, MemoryBoardState, MemoryBoardUnitOfWork
from wf.application.board.tickets import TicketCommands
from wf.application.execution.agent import AgentRunService
from wf.domain.board.errors import BoardError
from wf.domain.execution import agent_run
from wf.domain.execution.agent_run import RunResult


def detail(stage="in_dev", comments=()):
    return {"ticket": {"key": "K-1", "summary": "add search", "jira_status": "In Dev", "stage": stage,
                       "description": "make it", "acceptance": "it works"}, "comments": list(comments)}


def test_the_brief_has_the_ticket_notes_for_the_stage_and_the_last_ten_comments():
    comments = [{"author": f"a{i}", "body": f"c{i}"} for i in range(12)] + [{"author": "z", "body": ""}]
    text = agent_run.brief(detail("regression", comments))
    assert text.startswith("Ticket K-1: add search\nStatus in Jira: In Dev. Internal stage: regression.")
    assert "This is the regression stage on dev." in text and "Acceptance criteria:\nit works" in text
    assert "- a2: c2" in text and "- a1: c1" not in text and "z:" not in text
    assert "(none)" in agent_run.brief({"ticket": {**detail()["ticket"], "description": None}})


def test_the_system_prompt_gains_the_bot_account_only_when_there_is_one():
    assert agent_run.compose_system_prompt("base") == "base"
    text = agent_run.compose_system_prompt("base\n", "qa@bots.test", "/creds")
    assert text.startswith("base\n\n## Your app account") and "qa@bots.test" in text and "/creds" in text


def env(**kw):
    base = dict(role="qa", key="K-1", db_path="/db", worktree=Path("/wt"), test_cmd="t", app_cmd="a",
                app_url="http://x", bot_email="", credentials_path="", regression_url="", on_dev=False)
    return agent_run.gateway_env(**{**base, **kw})


def test_the_gateway_environment_carries_the_scope_and_the_secret_goes_to_the_dev_host_only():
    e = env()
    assert e["WF_ROLE"] == "qa" and e["WF_DB"] == "/db" and e["WF_WORKTREE"] == "/wt"
    assert "WF_RUNNER_SECRET" not in e and "WF_BOT_APP" not in e
    assert "WF_RUNNER_SECRET" not in env(runner_secret="s")  # no regression url: nothing to send it to
    e = env(runner_secret="s", regression_url="https://dev.example.test/app", on_dev=True)
    assert e["WF_RUNNER_SECRET"] == "s" and e["WF_RUNNER_SECRET_HOST"] == "dev.example.test"
    assert e["WF_BOT_APP"] == "https://dev.example.test/app"


class Workspace:
    def __init__(self, sync_error=None):
        self.sync_error, self.synced = sync_error, []

    def ensure_worktree(self, key): return Path("/wt") / key
    def sync_with_main(self, key):
        self.synced.append(key)
        if self.sync_error:
            raise RuntimeError(self.sync_error)


class Runner:
    def __init__(self): self.requests = []

    def run(self, request):
        self.requests.append(request)
        return RunResult(0, False, request.run_dir / "output.jsonl", None)


class Prompts:
    def role_prompt(self, role): return f"you are {role}"


class Queries:
    def __init__(self, detail_): self._d = detail_
    def ticket_detail(self, key): return self._d
    def database_path(self): return "/db"


@pytest.fixture
def parts():
    state = MemoryBoardState()
    state.add_ticket("K-1", "regression")
    commands = TicketCommands(lambda: MemoryBoardUnitOfWork(state), lambda: 1000.0)
    runs, runner, ws = MemoryAgentRuns(), Runner(), Workspace()
    svc = AgentRunService(commands=commands, queries=Queries(detail("regression")), runs=runs, workspace=ws,
                          runner=runner, prompts=Prompts(), runs_dir=Path("/runs"), clock=lambda: 1000.0)
    return state, svc, runs, runner, ws


def test_a_run_briefs_the_role_scopes_the_gateway_and_is_recorded(parts):
    state, svc, runs, runner, ws = parts
    result = svc.run("qa", "K-1", access=True, model="m", app_url="http://local", regression_url="https://dev.test",
                     bot_domain="bots.test", bot_credentials="/c", runner_secret="s")
    req = runner.requests[0]
    assert req.role == "qa" and req.access and req.model == "m" and req.worktree == Path("/wt/K-1")
    assert req.run_dir == Path("/runs/runs/K-1/qa-1000") and req.system_prompt.startswith("you are qa")
    assert "qa@bots.test" in req.system_prompt and req.prompt.startswith("Ticket K-1")
    assert req.gateway_env["WF_APP_URL"] == "https://dev.test"  # regression runs against dev
    assert req.gateway_env["WF_RUNNER_SECRET_HOST"] == "dev.test"
    assert (result.stage, result.role, result.run.exit_code) == ("regression", "qa", 0)
    assert runs.last_run("K-1", "regression")["role"] == "qa" and ws.synced == []  # only dev syncs


def test_a_dev_run_syncs_with_main_first_and_a_failed_sync_flags_the_ticket(parts):
    state, svc, runs, runner, _ = parts
    svc._workspace = Workspace(sync_error="could not rebase")
    result = svc.run("dev", "K-1", access=True, model="m")
    assert result.run is None and runner.requests == []
    assert state.tickets["K-1"].flags == ["blocked"] and runs.last_run("K-1", "regression") is None
    svc._workspace = Workspace()
    assert svc.run("dev", "K-1", access=True, model="m").run is not None and svc._workspace.synced == ["K-1"]


def test_an_unknown_ticket_is_refused(parts):
    state, svc, *_ = parts
    svc._queries = Queries(None)
    with pytest.raises(BoardError, match="no ticket K-1"):
        svc.run("qa", "K-1", access=True, model="m")
