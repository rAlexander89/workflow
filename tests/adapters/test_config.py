import pytest
from wf.adapters.config import workspace as c


def test_add_repo_dedupes_and_validates():
    cfg = c.Config()
    assert cfg.add_repo("api", "/srv/wf/api.git") is True
    assert cfg.add_repo("api", "/elsewhere") is False
    with pytest.raises(c.ConfigError):
        cfg.add_repo("Not A Name", "/srv/x")
    with pytest.raises(c.ConfigError):
        cfg.add_repo("web", "  ")


def test_board_refs_must_be_numeric_jira_ids():
    cfg = c.Config()
    assert cfg.add_board("jira", "12") is True
    with pytest.raises(c.ConfigError):
        cfg.add_board("jira", "ABC-board")
    with pytest.raises(c.ConfigError):
        cfg.add_board("github", "acme/3")


def test_remove(tmp_path):
    cfg = c.Config()
    cfg.add_repo("api", "/srv/wf/api.git")
    cfg.add_board("jira", "12")
    assert cfg.remove_repo("api") is True
    assert cfg.remove_repo("api") is False
    assert cfg.remove_board("jira", "12") is True


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "workflow.json"
    cfg = c.Config()
    cfg.add_repo("api", "/srv/wf/api.git")
    cfg.add_board("jira", "12", label="Platform")
    c.save(cfg, path)
    loaded = c.load(path)
    assert loaded.to_dict() == cfg.to_dict()


def test_load_missing_file_is_empty(tmp_path):
    assert c.load(tmp_path / "nope.json").to_dict() == {
        "repos": [],
        "boards": [],
        "model": {
            "provider": "ollama",
            "model": "qwen3.5:9b",
            "base_url": "http://127.0.0.1:11434",
            "api_key_env": "ANTHROPIC_API_KEY",
            "claude_code_access": False,
        },
        "agents": {"runs_dir": "~/wf-runs", "test_cmd": "make test", "model": "claude-sonnet-5-5", "app_cmd": "", "app_url": "", "regression_url": "", "auto_run": False},
        "bots": {"enabled": False, "domain": "", "roles": [], "credentials_file": ""},
    }


def test_load_rejects_bad_file(tmp_path):
    path = tmp_path / "workflow.json"
    path.write_text("{not json")
    with pytest.raises(c.ConfigError):
        c.load(path)


def test_file_holds_no_credentials(tmp_path):
    path = tmp_path / "workflow.json"
    cfg = c.Config()
    cfg.add_board("jira", "12")
    c.save(cfg, path)
    assert "TOKEN" not in path.read_text().upper()
