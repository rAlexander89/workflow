import subprocess

import pytest

from wf.adapters.workspace import onboarding as onboarding
from wf.entrypoints import cli
from wf.adapters.config import workspace as cfgmod


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def make_repo(path, email, commits=1, origin=None):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for i in range(commits):
        git(path, "-c", f"user.email={email}", "-c", "user.name=Tester", "commit",
            "-q", "--allow-empty", "-m", f"c{i}")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/acme/api.git", "acme/api"),
    ("git@github.com:acme/api.git", "acme/api"),
    ("https://example.com/acme/api/", "acme/api"),
    ("", ""),
])
def test_parse_remote(url, expected):
    assert onboarding.parse_remote(url) == expected


def test_suggestions_put_the_repos_you_work_on_first(tmp_path):
    root = tmp_path / "code"
    make_repo(root / "api", "me@example.com", commits=3, origin="https://github.com/acme/api.git")
    make_repo(root / "web", "someone@else.com", commits=5, origin="https://github.com/acme/web.git")
    make_repo(root / "notes", "me@example.com", commits=1)
    found = onboarding.suggest([root], email="me@example.com")
    assert [c.name for c in found] == ["api", "notes", "web"]
    assert found[0].remote == "acme/api" and found[0].my_commits == 3
    assert found[1].remote == ""  # no origin: the user is asked for it


def test_nested_repos_inside_a_repo_are_not_listed_twice(tmp_path):
    root = tmp_path / "code"
    make_repo(root / "outer", "me@example.com")
    make_repo(root / "outer" / "vendor" / "inner", "me@example.com")
    assert [p.name for p in onboarding.find_repos([root])] == ["outer"]


def test_init_refuses_without_an_interactive_terminal(tmp_path, capsys):
    cfg = tmp_path / "workflow.json"
    assert cli.main(["--config", str(cfg), "init"]) == 2
    assert "--mcp-connected" in capsys.readouterr().err


def test_init_lists_repos_then_links_the_picked_ones(tmp_path, capsys, monkeypatch):
    root = tmp_path / "code"
    make_repo(root / "api", "me@example.com", commits=2, origin="git@github.com:acme/api.git")
    make_repo(root / "web", "someone@else.com", commits=1, origin="https://github.com/acme/web.git")
    monkeypatch.setattr(onboarding, "user_email", lambda: "me@example.com")
    cfg = tmp_path / "workflow.json"

    assert cli.main(["--config", str(cfg), "init", "--mcp-connected", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "acme/api" in out and "Re-run with --pick" in out

    assert cli.main(["--config", str(cfg), "init", "--mcp-connected", "--root", str(root), "--pick", "1"]) == 0
    repos = cfgmod.load(cfg).repos
    assert [(r.name, r.remote) for r in repos] == [("api", "acme/api")]
    assert repos[0].path.endswith("api")


def test_init_rejects_a_pick_that_is_not_in_the_list(tmp_path, capsys, monkeypatch):
    root = tmp_path / "code"
    make_repo(root / "api", "me@example.com", origin="https://github.com/acme/api.git")
    monkeypatch.setattr(onboarding, "user_email", lambda: "me@example.com")
    cfg = tmp_path / "workflow.json"
    assert cli.main(["--config", str(cfg), "init", "--mcp-connected", "--root", str(root), "--pick", "9"]) == 2
    assert "not one of the numbers" in capsys.readouterr().err


def test_source_control_non_interactive_infers_the_forge():
    from wf.adapters.workspace import onboarding as onboarding
    from wf.entrypoints import cli
    gh = onboarding.RepoCandidate(path="/x", name="a", remote="o/a", last_commit=0, my_commits=0,
                                  remote_url="git@github.com:o/a.git")
    gl = onboarding.RepoCandidate(path="/y", name="b", remote="g/b", last_commit=0, my_commits=0,
                                  remote_url="git@git.example.com:g/b.git")
    local = onboarding.RepoCandidate(path="/z", name="c", remote="", last_commit=0, my_commits=0, remote_url="")
    assert cli._source_control(gh, interactive=False) == ("github", "", "o/a")
    assert cli._source_control(gl, interactive=False) == ("gitlab", "", "g/b")
    assert cli._source_control(local, interactive=False) == ("local", "", "")


def test_source_control_interactive_asks_the_four_options(monkeypatch):
    from wf.adapters.workspace import onboarding as onboarding
    from wf.entrypoints import cli
    answers = iter(["2", "git.example.com", "g/b"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    c = onboarding.RepoCandidate(path="/y", name="b", remote="", last_commit=0, my_commits=0, remote_url="")
    assert cli._source_control(c, interactive=True) == ("gitlab", "git.example.com", "g/b")
