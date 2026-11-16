"""Stage order and labels for the pipeline views, in the order a ticket moves through."""

from __future__ import annotations

from wf import agents, mapping

STAGE_INFO: dict[str, tuple[str, str]] = {
    "backlog": ("Backlog", "Not started"),
    "needs_refinement": ("Needs refinement", "Product"),
    "in_refinement": ("In refinement", "Staff"),
    "hotfix_review": ("Hotfix review", "Staff"),
    "ready_for_dev": ("Ready for dev", "Dev"),
    "in_dev": ("In development", "Dev"),
    "pr_open": ("PR open", "PR review"),
    "pr_in_review": ("PR in review", "PR review"),
    "ready_for_qa": ("Ready for QA", "QA"),
    "qa_approved": ("QA approved", "Merge gate"),
    "merged": ("Merged to main", "Harness"),
    "regression": ("Regression", "QA"),
    "performance": ("Performance", "Performance bot"),
    "deployed": ("Deployed to prod", "Person"),
}


def ordered_stages() -> list[dict]:
    """Every stage in workflow order, with its label, owner, and agent role."""
    out = []
    for name in mapping.STAGES:
        label, owner = STAGE_INFO.get(name, (name, ""))
        out.append({"name": name, "label": label, "owner": owner,
                    "role": agents.STAGE_ROLE.get(name)})
    return out


def progress(current: str | None) -> list[dict]:
    """The stages with each one marked done, current, or to come, for a ticket's timeline."""
    stages = ordered_stages()
    names = [s["name"] for s in stages]
    idx = names.index(current) if current in names else -1
    for i, s in enumerate(stages):
        s["state"] = "done" if i < idx else "current" if i == idx else "todo"
    return stages


# The steps a ticket goes through after it leaves the dev stages, in order. Each step is
# tied to the internal stage that means "this step is happening now".
FLOW = [
    ("dev_work", "Dev", "in_dev"),
    ("pr_review", "Internal PR review", "pr_open"),
    ("qa", "QA", "ready_for_qa"),
    ("github", "GitHub PR", None),
    ("main", "In main (GitHub)", None),
    ("dev", "Deployed to dev", None),
    ("regression", "Regression on dev", "regression"),
    ("performance", "Performance", "performance"),
    ("prod", "Deployed to prod", "deployed"),
]
_ORDER = [stage for _, _, stage in FLOW if stage]


def flow_states(stage: str | None, *, last_run_failed: bool, perf_requested: bool,
                pr_url: str | None, pr_merged: bool, pr_checks: str = "none", pr_closed: bool = False,
                prod_ready: bool = False, flagged: bool = False, dev_deployed: bool = False,
                prod_deployed: bool = False) -> list[dict]:
    """Each step with its state: done (green), current (yellow), failed (red), skipped, or todo.

    A step is failed only where the ticket is now and its last agent run failed. Performance is
    skipped unless the dev asked for it. "Deployed to dev" stays todo until the GitHub merge
    lands on main; the dev deploy is not yet reported back to the workflow.
    """
    pos = _ORDER.index(stage) if stage in _ORDER else -1
    out = []
    for key, label, at in FLOW:
        state = "todo"
        if key == "performance" and not perf_requested and (stage is None or pos >= _ORDER.index("regression")):
            state = "skipped"
        elif key == "github":
            if not pr_url:
                state = "todo"
            elif pr_merged:
                state = "done"
            elif pr_closed or pr_checks == "failed":
                state = "failed"
            else:
                state = "current"
        elif key == "main":
            state = "done" if pr_merged else "todo"
        elif key == "dev":
            state = "done" if (dev_deployed or stage in ("regression", "performance", "deployed")) else "todo"
        elif key == "dev_work":
            if stage == "in_dev":
                state = "failed" if (flagged or last_run_failed) else "current"
            elif stage in ("pr_open", "pr_in_review", "ready_for_qa", "qa_approved", "merged", "regression",
                           "performance", "deployed"):
                state = "done"
        elif key == "pr_review" and stage in ("ready_for_qa", "qa_approved", "merged", "regression", "performance", "deployed"):
            state = "done"
        elif key == "qa":
            if stage == "ready_for_qa":
                state = "failed" if last_run_failed else "current"
            elif stage in ("qa_approved", "merged", "regression", "performance", "deployed"):
                state = "done"
        elif at is not None and stage == at:
            state = "failed" if last_run_failed else "current"
        elif at is not None and pos >= 0 and _ORDER.index(at) < pos:
            state = "done"
        elif at is not None and stage == "deployed":
            state = "done"
        if key == "prod":
            state = "done" if prod_deployed else ("current" if (stage == "deployed" or prod_ready) else "todo")
        out.append({"key": key, "label": label, "state": state})
    return out
