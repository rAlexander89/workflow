"""Run registry: one row per workflow run, for the run list and status pages.

The LangGraph checkpoints live in the same SQLite file under their own tables.
This table holds what the web pages need without replaying the graph.
The SQLite implementation is in `wf.adapters.persistence.sqlite.runs`.
"""

from wf.adapters.persistence.sqlite.runs import SCHEMA, SqliteRunRegistry as RunStore  # noqa: F401
from wf.domain.execution.run import Run  # noqa: F401
