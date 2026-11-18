"""Local token-usage ledger and efficiency audit.

Every model call an agent makes is recorded here, in the same local SQLite file as
the rest of the workflow record. Nothing leaves the machine. The audit reads the
ledger and reports, per agent: call counts, average input size, cache hit ratio,
and the largest calls, so prompt bloat and missed caching show up.

Cost is left null on purpose. Model prices change, so they belong in config once
we pick the models, not hard-coded here.
"""

from __future__ import annotations

from wf.adapters.persistence.sqlite.usage import SCHEMA, SqliteUsageLog as UsageLedger  # noqa: F401
from wf.domain.execution.usage import AgentAudit, UsageRecord  # noqa: F401


def format_audit(rows: list[AgentAudit], largest: list[dict]) -> str:
    if not rows:
        return "no model calls recorded yet"
    lines = [
        f"{'agent':<16}{'calls':>7}{'avg in':>10}{'avg out':>10}{'total in':>12}{'total out':>12}{'cache hit':>11}",
    ]
    for a in rows:
        lines.append(
            f"{a.agent:<16}{a.calls:>7}{a.avg_input:>10.0f}{a.avg_output:>10.0f}"
            f"{a.total_input:>12}{a.total_output:>12}{a.cache_hit_ratio:>10.0%}"
        )
    if largest:
        lines.append("")
        lines.append("largest calls (input + output):")
        for r in largest:
            lines.append(
                f"  {r['agent']:<14} {r['ticket'] or '-':<12} {r['model']:<22} "
                f"in {r['input_tokens']:>7}  out {r['output_tokens']:>6}"
            )
    return "\n".join(lines)
