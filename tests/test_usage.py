import pytest

from wf.usage import UsageLedger, UsageRecord, format_audit


def test_empty_ledger_says_so(tmp_path):
    ledger = UsageLedger(tmp_path / "wf.sqlite")
    assert format_audit(ledger.audit(), ledger.largest_calls()) == "no model calls recorded yet"


def test_audit_per_agent(tmp_path):
    ledger = UsageLedger(tmp_path / "wf.sqlite")
    ledger.record(UsageRecord("staff", "m", input_tokens=1000, output_tokens=200, cache_read_tokens=3000))
    ledger.record(UsageRecord("staff", "m", input_tokens=500, output_tokens=100))
    ledger.record(UsageRecord("qa", "m", input_tokens=200, output_tokens=50))

    by_agent = {a.agent: a for a in ledger.audit()}
    staff = by_agent["staff"]
    assert staff.calls == 2
    assert staff.avg_input == 750
    assert staff.total_output == 300
    # cache reads 3000 of (1000 + 500 + 3000) input-side tokens
    assert staff.cache_hit_ratio == pytest.approx(3000 / 4500)
    assert by_agent["qa"].cache_hit_ratio == 0


def test_largest_calls_sorted_by_total(tmp_path):
    ledger = UsageLedger(tmp_path / "wf.sqlite")
    ledger.record(UsageRecord("qa", "m", input_tokens=10, output_tokens=5, ticket="A"))
    ledger.record(UsageRecord("dev", "m", input_tokens=9000, output_tokens=900, ticket="B"))
    top = ledger.largest_calls(limit=1)
    assert top[0]["agent"] == "dev" and top[0]["ticket"] == "B"


def test_negative_counts_rejected(tmp_path):
    ledger = UsageLedger(tmp_path / "wf.sqlite")
    with pytest.raises(ValueError):
        ledger.record(UsageRecord("qa", "m", input_tokens=-1, output_tokens=0))


def test_cli_usage_prints_audit(tmp_path, capsys):
    from wf.entrypoints import cli

    db = tmp_path / "wf.sqlite"
    UsageLedger(db).record(UsageRecord("product", "m", input_tokens=100, output_tokens=20))
    assert cli.main(["usage", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "product" in out and "cache hit" in out
