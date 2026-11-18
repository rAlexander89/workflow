import json

import pytest

from wf.adapters.config.workspace import Config, ModelConfig
from wf.adapters.model import build_model, known_providers
from wf.domain.execution.model import Message, ModelError, ModelRequest, ProviderNotAllowed
from wf.adapters.model.anthropic_api import API_URL, AnthropicAPIModel
from wf.adapters.model.ollama import OllamaModel
from wf.adapters.model.recording import record_call
from tests.support.usage import UsageLedger

REQ = ModelRequest(system="be brief", messages=(Message("user", "hi"),), max_tokens=64)


def test_ollama_maps_request_and_usage():
    seen = {}

    def fake(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": "hello"},
            "prompt_eval_count": 120,
            "eval_count": 7,
        }

    cfg = ModelConfig(provider="ollama", model="qwen3.5:9b", base_url="http://127.0.0.1:11434/")
    model = build_model(cfg, env={}, transport=fake)
    assert isinstance(model, OllamaModel)
    resp = model.complete(REQ)

    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["body"]["options"]["num_predict"] == 64
    assert resp.text == "hello"
    assert (resp.input_tokens, resp.output_tokens) == (120, 7)


def test_anthropic_sends_key_in_header_and_maps_cache_usage():
    seen = {}

    def fake(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return {
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 40,
                "output_tokens": 9,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 60,
            },
        }

    cfg = ModelConfig(provider="anthropic-api", model="claude-sonnet-5-5", api_key_env="WF_KEY")
    model = build_model(cfg, env={"WF_KEY": "sk-test-123"}, transport=fake)
    assert isinstance(model, AnthropicAPIModel)
    resp = model.complete(REQ)

    assert seen["url"] == API_URL
    assert seen["headers"]["x-api-key"] == "sk-test-123"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["system"] == "be brief"
    assert resp.text == "ok"
    assert (resp.input_tokens, resp.output_tokens) == (40, 9)
    assert (resp.cache_read_tokens, resp.cache_write_tokens) == (500, 60)


def test_anthropic_without_key_fails_clearly_and_does_not_leak():
    cfg = ModelConfig(provider="anthropic-api", model="m", api_key_env="WF_KEY")
    with pytest.raises(ModelError, match="WF_KEY is not set"):
        build_model(cfg, env={})




def test_unknown_provider_lists_options():
    cfg = ModelConfig(provider="gemini", model="m")
    with pytest.raises(ProviderNotAllowed, match="ollama"):
        build_model(cfg, env={})
    assert known_providers() == ["anthropic-api", "claude-code", "ollama"]


def test_call_is_recorded_in_ledger(tmp_path):
    def fake(url, headers, body):
        return {
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 90},
        }

    cfg = ModelConfig(provider="anthropic-api", model="claude-sonnet-5-5", api_key_env="K")
    model = build_model(cfg, env={"K": "k"}, transport=fake)
    ledger = UsageLedger(tmp_path / "wf.sqlite")
    record_call(ledger, "staff", model.complete(REQ), ticket="ABC-1", provider=model.name)

    row = ledger.largest_calls(1)[0]
    assert row["agent"] == "staff" and row["ticket"] == "ABC-1"
    assert ledger.audit()[0].cache_hit_ratio == pytest.approx(90 / 100)


def test_model_config_round_trips_and_defaults_to_local(tmp_path):
    assert Config().model.provider == "ollama"
    cfg = Config()
    cfg.model = ModelConfig(provider="anthropic-api", model="claude-haiku-5-5", api_key_env="ANTHROPIC_API_KEY")
    restored = Config.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert restored.model == cfg.model
    # An old config file with no "model" key still loads, with the local default.
    assert Config.from_dict({"repos": [], "boards": []}).model.provider == "ollama"


def test_config_never_stores_the_key_itself():
    data = Config(model=ModelConfig(provider="anthropic-api", model="m")).to_dict()
    assert "sk-" not in json.dumps(data)
    assert data["model"]["api_key_env"] == "ANTHROPIC_API_KEY"


def test_cli_ask_routes_through_engine_and_records(tmp_path, monkeypatch, capsys):
    from wf.entrypoints import cli
    from wf.adapters.model import anthropic_api

    cfg_path = tmp_path / "workflow.json"
    assert cli.main(["--config", str(cfg_path), "model", "set", "--provider", "anthropic-api",
                     "--model", "claude-haiku-5-5"]) == 0

    calls = []

    def fake_post(url, headers, body, timeout=300.0):
        calls.append((url, headers, body))
        return {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 12, "output_tokens": 1}}

    monkeypatch.setattr(anthropic_api, "post_json", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-xyz")
    capsys.readouterr()  # drop the output of `model set`
    db = tmp_path / "wf.sqlite"
    assert cli.main(["--config", str(cfg_path), "ask", "ping", "--agent", "staff",
                     "--ticket", "ABC-9", "--db", str(db)]) == 0

    out, err = capsys.readouterr()
    assert out.strip() == "pong"
    assert "sk-test-xyz" not in out + err  # the key never reaches the terminal
    assert calls[0][2]["model"] == "claude-haiku-5-5"
    from tests.support.usage import UsageLedger
    assert UsageLedger(db).largest_calls(1)[0]["ticket"] == "ABC-9"


def test_cli_status_never_prints_the_key(tmp_path, monkeypatch, capsys):
    from wf.entrypoints import cli

    cfg_path = tmp_path / "workflow.json"
    cli.main(["--config", str(cfg_path), "model", "set", "--provider", "anthropic-api"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    assert cli.main(["--config", str(cfg_path), "model", "status"]) == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY is set" in out
    assert "sk-secret-value" not in out
