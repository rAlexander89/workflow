"""Write every model call to the token ledger, whichever adapter made it."""

from __future__ import annotations

from wf.model import ModelResponse
from wf.usage import UsageLedger, UsageRecord


def record_call(
    ledger: UsageLedger,
    agent: str,
    response: ModelResponse,
    *,
    ticket: str | None = None,
    run_id: str | None = None,
    provider: str | None = None,
) -> None:
    ledger.record(
        UsageRecord(
            agent=agent,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_write_tokens,
            run_id=run_id,
            ticket=ticket,
            duration_ms=response.duration_ms,
            note=f"provider={provider}" if provider else None,
        )
    )
