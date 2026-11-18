"""Reading deploys: what a ticket's observation notes say, and what a forge's CI runs say."""

from __future__ import annotations

DEV_WORKFLOW = "Build and Deploy"
PROD_WORKFLOW = "Deploy to prod"
FAILED_CONCLUSIONS = ("failure", "cancelled", "timed_out", "startup_failure")


def latest(notes: list[str], prefix: str) -> str | None:
    """The most recent observation (notes are newest first) whose text starts with prefix, e.g. 'dev:'."""
    for note in notes:
        if note and note.startswith(prefix):
            return note
    return None


def is_dev_running(notes: list[str]) -> bool:
    return bool(latest(notes, "dev:")) and latest(notes, "dev:").startswith("dev:running:")


def is_dev_deployed(notes: list[str]) -> bool:
    return bool(latest(notes, "dev:")) and latest(notes, "dev:").startswith("dev:success:")


def is_prod_deployed(notes: list[str]) -> bool:
    return bool(latest(notes, "prod:")) and latest(notes, "prod:").startswith("prod:success:")


def sha_of(note: str) -> str:
    """The commit in a note like 'dev:success:<sha>' or 'prod:started:<sha>'."""
    return note.split(":", 2)[2]


def workflow_name(forge_name: str, name: str) -> str | None:
    """GitHub runs are named by their workflow file; a GitLab pipeline is found by its commit."""
    return name if forge_name == "github" else None


def workflow_state(runs: list[dict], sha: str, workflow: str | None) -> str:
    """'waiting', 'running', 'failed', or 'success' for the runs of this commit (and workflow, if given)."""
    runs = [r for r in runs if r.get("sha") == sha and (workflow is None or r.get("name") == workflow)]
    if not runs:
        return "waiting"
    if any(r.get("conclusion") in FAILED_CONCLUSIONS for r in runs):
        return "failed"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    return "success"


def failure_entry(run: dict, job: dict, messages: list[str]) -> dict:
    """One line of a failure report: the failed job, its link, and the messages the forge attached."""
    messages = [m for m in messages if m]
    return {"step": run.get("name") or "deploy", "job": job.get("name", ""),
            "url": job.get("url") or run.get("url", ""),
            "reasons": messages[:3] or ["no message was attached to this job; open it on the forge"]}


# --- publishing a pull request ---------------------------------------------------------------

PUBLISHABLE_STAGES = ("qa_approved", "merged", "regression", "performance", "deployed")


def publish_stage_block(key: str, stage: str) -> str | None:
    """A ticket may be published only after it has passed QA."""
    return None if stage in PUBLISHABLE_STAGES else f"{key} is in {stage}; it must pass QA first"
