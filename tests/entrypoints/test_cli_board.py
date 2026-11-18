"""The board commands of the CLI: sync, backlog, status, move, and flag."""
import json

from tests.builders import NOW, STATUSES, backlog_ticket, clock, payload, sprint, sync, ticket  # noqa: F401
from wf.adapters.persistence.sqlite.schema import connect
from wf.entrypoints.cli import main  # noqa: F401
from wf.entrypoints import cli


def test_cli_sync_board_and_sprints(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps(payload(tickets=[ticket("ABC-1")])))
    assert cli.main(["sync", str(f), "--db", db]) == 0
    assert "tickets_created=1" in capsys.readouterr().out

    assert cli.main(["board", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "ready_for_dev" in out and "ABC-1" in out

    assert cli.main(["sprints", "--state", "active", "--db", db]) == 0
    assert "Sprint 101" in capsys.readouterr().out


def test_cli_sync_reports_a_bad_file(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    assert cli.main(["sync", str(f), "--db", db]) == 2
    assert "could not read" in capsys.readouterr().err


def test_cli_backlog_lists_tickets(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "backlog.json"
    f.write_text(json.dumps(payload(kind="backlog", tickets=[backlog_ticket("ABC-5")])))
    assert cli.main(["sync", str(f), "--db", db]) == 0
    capsys.readouterr()
    assert cli.main(["backlog", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "ABC-5" in out and "ready_for_dev" not in out


def test_cli_status_flags_a_stale_board(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps(payload(tickets=[ticket("ABC-1")])))
    cli.main(["sync", str(f), "--db", db])
    capsys.readouterr()
    cli.main(["status", "--db", db])  # real clock: the sync just happened
    assert "stale" not in capsys.readouterr().out


def test_cli_move_and_flag(tmp_path, capsys):
    db = str(tmp_path / "wf.sqlite")
    f = tmp_path / "sync.json"
    f.write_text(json.dumps(payload(tickets=[ticket("ABC-1", status="Ready for dev")])))
    cli.main(["sync", str(f), "--db", db])
    capsys.readouterr()
    assert cli.main(["move", "ABC-1", "pr_open", "--actor", "dev", "--reason", "opened PR", "--db", db]) == 0
    assert cli.main(["move", "ABC-1", "regression", "--actor", "qa", "--reason", "x", "--db", db]) == 2
    assert cli.main(["flag", "ABC-1", "needs-human", "--actor", "dev", "--reason", "unclear", "--db", db]) == 0
    assert cli.main(["flag", "ABC-1", "needs-human", "--actor", "dev", "--clear", "--db", db]) == 2
    assert cli.main(["flag", "ABC-1", "needs-human", "--actor", "user", "--clear", "--db", db]) == 0
    assert "error" in capsys.readouterr().err
