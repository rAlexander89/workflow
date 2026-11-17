"""Who may move a ticket from one internal stage to another."""

from __future__ import annotations

FLAGS = ("needs-human", "blocked")

# Who may move a ticket from one internal stage to another. Derived from the agent
# prompts. The user may move anything. This list is not complete: add a row when a
# prompt gains a new allowed move. Anything not listed is refused.
ALLOWED_MOVES: frozenset[tuple[str, str, str]] = frozenset({
    ("staff", "in_refinement", "ready_for_dev"),
    ("harness", "backlog", "in_refinement"),
    ("harness", "in_dev", "ready_for_dev"),
    ("harness", "pr_open", "ready_for_dev"),
    ("harness", "pr_in_review", "ready_for_dev"),
    ("dev", "ready_for_dev", "in_dev"),
    ("dev", "ready_for_dev", "pr_open"),
    ("dev", "in_dev", "pr_open"),
    ("dev", "pr_in_review", "in_dev"),
    ("dev", "qa_approved", "merged"),
    ("pr_review", "pr_open", "in_dev"),
    ("pr_review", "pr_in_review", "in_dev"),
    ("qa", "ready_for_qa", "qa_approved"),
    ("qa", "ready_for_qa", "in_dev"),
    ("qa", "regression", "in_dev"),
    ("harness", "pr_in_review", "ready_for_qa"),
    ("pr_review", "pr_open", "ready_for_perf_review"),
    ("pr_review", "pr_in_review", "ready_for_perf_review"),
    ("harness", "pr_open", "ready_for_perf_review"),
    ("harness", "pr_in_review", "ready_for_perf_review"),
    ("harness", "ready_for_perf_review", "ready_for_qa"),
    ("perf_review", "ready_for_perf_review", "ready_for_qa"),
    ("perf_review", "ready_for_perf_review", "in_dev"),
    ("qa", "regression", "deployed"),
    ("harness", "merged", "regression"),
    ("harness", "regression", "deployed"),
    ("harness", "performance", "deployed"),
    ("qa", "regression", "performance"),
    ("harness", "regression", "performance"),
    ("harness", "qa_approved", "merged"),
})


def may_move(actor: str, from_stage: str | None, to_stage: str) -> bool:
    if actor == "user":
        return True
    return (actor, from_stage, to_stage) in ALLOWED_MOVES
