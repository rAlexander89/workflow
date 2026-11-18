from wf.entrypoints import cli


def run(tmp_path, *args):
    return cli.main(["--config", str(tmp_path / "workflow.json"), *args])


def test_link_list_unlink(tmp_path, capsys):
    assert run(tmp_path, "link", "repo", "api", "/srv/wf/api.git") == 0
    assert run(tmp_path, "link", "board", "jira", "12", "--label", "Platform") == 0
    capsys.readouterr()
    assert run(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "repo   api" in out and "/srv/wf/api.git" in out
    assert "board  jira   12  (Platform)" in out

    assert run(tmp_path, "unlink", "jira", "12") == 0
    assert run(tmp_path, "unlink", "jira", "12") == 1


def test_bad_input_returns_error_code(tmp_path, capsys):
    assert run(tmp_path, "link", "repo", "Bad Name", "/srv/x") == 2
    assert "lowercase" in capsys.readouterr().err


def test_relinking_is_idempotent(tmp_path, capsys):
    run(tmp_path, "link", "repo", "api", "/srv/wf/api.git")
    capsys.readouterr()
    assert run(tmp_path, "link", "repo", "api", "/srv/wf/api.git") == 0
    assert "already linked" in capsys.readouterr().out


def test_run_and_resume_through_cli(tmp_path, capsys):
    db = str(tmp_path / "runs.sqlite")
    assert cli.main(["run", "add search", "--thread", "t1", "--db", db]) == 0
    assert "waiting_for_approval" in capsys.readouterr().out
    assert cli.main(["resume", "t1", "--db", db, "--approve"]) == 0
    out = capsys.readouterr().out
    assert "steps: product -> staff -> gate -> dev -> qa" in out
    assert "status: done" in out
