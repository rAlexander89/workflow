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
    "deployed": ("Deployed", "Performance"),
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
