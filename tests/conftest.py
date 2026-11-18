import pytest

from tests.builders import make_container, make_repo
from wf.adapters.persistence.sqlite.schema import connect


@pytest.fixture
def conn(tmp_path):
    """A connection to a fresh board database, for tests of the SQLite adapters."""
    c = connect(tmp_path / "wf.sqlite")
    yield c
    c.close()


@pytest.fixture
def repo(tmp_path):
    """A git repository with one commit on main."""
    return make_repo(tmp_path / "internal")


@pytest.fixture
def container(tmp_path, repo):
    """A container over a fresh database, linked to `repo`, with no outside access faked."""
    return make_container(tmp_path, repo=repo)


# --- the use cases and adapters over `conn`, for tests that exercise them against SQLite ---------------

from tests.builders import clock  # noqa: E402
from wf.adapters.persistence.sqlite.board import SqliteBoardUnitOfWork  # noqa: E402
from wf.adapters.persistence.sqlite.board_queries import SqliteBoardQueries  # noqa: E402
from wf.adapters.persistence.sqlite.board_sync import SqliteBoardSync  # noqa: E402
from wf.application.board.tickets import TicketCommands  # noqa: E402
from wf.application.tracker.outbox import OutboxService  # noqa: E402
from wf.bootstrap import Session, build_event_bus  # noqa: E402


@pytest.fixture
def tickets(conn):
    return TicketCommands(lambda: SqliteBoardUnitOfWork(conn), clock, build_event_bus())


@pytest.fixture
def queries(conn):
    return SqliteBoardQueries(conn)


@pytest.fixture
def board_sync(conn):
    return SqliteBoardSync(conn)


@pytest.fixture
def outbox_service(conn):
    return OutboxService(lambda: SqliteBoardUnitOfWork(conn), clock, build_event_bus())


@pytest.fixture
def session(container, conn):
    """The container's use cases over the test's own connection."""
    return Session(container, conn)


@pytest.fixture
def settings(container):
    return container.settings()
