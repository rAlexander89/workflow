"""Every adapter and fake has each method of its port, with the same parameter names."""
import inspect

import pytest

from tests import fakes
from wf.adapters.persistence.sqlite import agent_runs, board, board_queries, board_sync, changes, lifecycle, pause, runs
from wf.adapters.persistence.sqlite import usage
from wf.adapters.credentials.bots import FileBotAccounts
from wf.adapters.env.local_app import LocalAppEnvironment
from wf.adapters.system.preflight import MacMachineChecks
from wf.adapters.workspace.onboarding import GitRepoFinder
from wf.application.ports import board as board_ports
from wf.application.ports import setup as setup_ports
from wf.application.ports import persistence as ports

PAIRS = [
    (ports.RunRegistry, runs.SqliteRunRegistry), (ports.RunRegistry, fakes.MemoryRunRegistry),
    (ports.UsageLog, usage.SqliteUsageLog), (ports.UsageLog, fakes.MemoryUsageLog),
    (ports.ChangeLog, changes.SqliteChangeLog), (ports.ChangeLog, fakes.MemoryChangeLog),
    (ports.LifecycleLog, lifecycle.SqliteLifecycleLog), (ports.LifecycleLog, fakes.MemoryLifecycleLog),
    (ports.PauseState, pause.SqlitePauseState), (ports.PauseState, fakes.MemoryPauseState),
    (ports.AgentRunRepository, agent_runs.SqliteAgentRuns), (ports.AgentRunRepository, fakes.MemoryAgentRuns),
    (board_ports.TicketRepository, board.SqliteTicketRepository),
    (board_ports.TicketRepository, fakes.MemoryTicketRepository),
    (board_ports.StageMappings, board.SqliteStageMappings), (board_ports.StageMappings, fakes.MemoryStageMappings),
    (board_ports.OutboxQueue, board.SqliteOutboxQueue), (board_ports.OutboxQueue, fakes.MemoryOutboxQueue),
    (board_ports.BoardSync, board_sync.SqliteBoardSync),
    (board_ports.BoardQueries, board_queries.SqliteBoardQueries),
    (setup_ports.BotAccounts, FileBotAccounts), (setup_ports.AppEnvironment, LocalAppEnvironment),
    (setup_ports.MachineChecks, MacMachineChecks), (setup_ports.RepoFinder, GitRepoFinder),
]


def methods(cls):
    return {name: fn for name, fn in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")}


@pytest.mark.parametrize("port,impl", PAIRS, ids=lambda x: x.__name__)
def test_the_implementation_matches_the_port(port, impl):
    have = methods(impl)
    for name, declared in methods(port).items():
        assert name in have, f"{impl.__name__} is missing {name}"
        want = list(inspect.signature(declared).parameters)
        got = list(inspect.signature(have[name]).parameters)
        assert got[: len(want)] == want, f"{impl.__name__}.{name}: {got} does not match {want}"
