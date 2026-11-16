"""Automatic mapping from internal stages to a project's Jira status names.

The workflow has a fixed set of stages. Each team's statuses are matched to them by
name. There are no default status names: a team's statuses come from its own
projects. Matching never guesses:

- a stage with exactly one matching status is mapped
- a stage with no match, or with several, is unmapped and blocked
- a status that matches more than one stage is left out and reported

Stage owners (which agent acts in a stage) live in `wf.orchestrator`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order is the order of the workflow. Patterns are matched against the normalized
# status name (lowercase, punctuation replaced by spaces).
STAGE_PATTERNS: list[tuple[str, str]] = [
    ("backlog", r"\bbacklog\b"),
    ("needs_refinement", r"\bneeds refinement\b|\bto refine\b"),
    ("in_refinement", r"\bin refinement\b"),
    ("hotfix_review", r"\bhotfix review\b"),
    ("ready_for_dev", r"\bready for dev\b|\bready for development\b|\bselected for development\b"),
    ("in_dev", r"\bdev in progress\b|\bin development\b|\bin dev\b|^in progress$"),
    ("pr_open", r"\bpr open\b|\bpull request open\b"),
    ("pr_in_review", r"\bpr in review\b|\bin review\b"),
    ("ready_for_qa", r"\bready for qa\b|\bready for test(ing)?\b"),
    ("qa_approved", r"\bqa approved\b|\bqa passed\b"),
    ("merged", r"\bmerged\b"),
    ("regression", r"\bregression\b"),
    ("performance", r"\bperformance\b"),
    ("deployed", r"\bdeployed\b|\breleased\b"),
]
STAGES = [stage for stage, _ in STAGE_PATTERNS]
_COMPILED = [(stage, re.compile(pattern)) for stage, pattern in STAGE_PATTERNS]


@dataclass(frozen=True)
class StageRow:
    stage: str
    status_name: str | None  # None when the stage is unmapped
    reason: str


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name.lower())).strip()


def stages_for(status_name: str) -> list[str]:
    norm = normalize(status_name)
    return [stage for stage, rx in _COMPILED if rx.search(norm)]


def map_project(status_names: list[str]) -> tuple[list[StageRow], list[str]]:
    """Map one project's statuses to stages.

    Returns one row per stage, plus a list of problems: statuses that matched more
    than one stage and were left out.
    """
    problems: list[str] = []
    candidates: dict[str, list[str]] = {stage: [] for stage in STAGES}
    for name in sorted(set(status_names)):
        matches = stages_for(name)
        if len(matches) > 1:
            problems.append(f"status {name!r} matches several stages ({', '.join(matches)}); left out")
        elif matches:
            candidates[matches[0]].append(name)

    rows: list[StageRow] = []
    for stage in STAGES:
        names = candidates[stage]
        if len(names) == 1:
            rows.append(StageRow(stage, names[0], f"matched {names[0]!r} by name"))
        elif not names:
            rows.append(StageRow(stage, None, "no status matches this stage"))
        else:
            rows.append(StageRow(stage, None, f"ambiguous: {', '.join(repr(n) for n in names)}"))
    return rows, problems
