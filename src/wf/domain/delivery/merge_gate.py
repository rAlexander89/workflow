"""The merge rules: when may a ticket's branch merge, and when is its pull request good to merge."""

from __future__ import annotations

from dataclasses import dataclass, field

from wf.domain.board.errors import BoardError
from wf.domain.delivery.roles import REQUIRED_APPROVALS, missing_approvals


class MergeBlocked(BoardError):
    pass


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)  # why not, when ok is False


# --- the internal merge gate -------------------------------------------------------------------

def stage_block(key: str, stage: str) -> str | None:
    """A ticket may merge only from qa_approved."""
    return None if stage == "qa_approved" else f"{key} is in {stage}, not qa_approved"


def approval_block(key: str, head: str, approved: set[str] | frozenset[str]) -> str | None:
    """Every required role must have approved the branch's current commit."""
    missing = missing_approvals(approved)
    if missing:
        return f"{key} at {head[:8]} is missing approval from {', '.join(sorted(missing))}"
    return None


def test_block(head: str, test_cmd: str, passed: bool | None, output: str = "") -> str | None:
    """The tests must be configured and pass at the current commit. `passed` is None when not run."""
    if not test_cmd:
        return "no test command is configured, so the tests cannot be checked"
    if passed is False:
        return f"tests fail at {head[:8]}: {output[-500:]}"
    return None


# --- the merge bot: is the GitHub PR good to merge ---------------------------------------------

@dataclass(frozen=True)
class PullRequestFacts:
    """Everything the merge bot looks at. None means the fact was not (or could not be) read."""
    key: str
    ticket_exists: bool = True
    flagged: bool = False
    pr_url: str | None = None
    pr: dict | None = None  # the forge's PR status: ok, merged, state, checks, commit_subjects, head, error
    branch_head: str | None = None
    approved_at_head: frozenset[str] = frozenset()
    # (ok, stray commit subjects) from comparing the branch with the remote main; None if not compared
    stray: tuple[bool, tuple[str, ...]] | None = None


def verdict(f: PullRequestFacts) -> Verdict:
    """Good to merge means: the PR is open with green checks and only this ticket's commits, its head is
    the branch's current commit, nothing is flagged, and every required role approved that commit."""
    if not f.ticket_exists:
        return Verdict(False, [f"no ticket {f.key}"])
    reasons: list[str] = []
    if f.flagged:
        reasons.append("the ticket is flagged; a person has to clear it")
    if f.pr_url is None:
        return Verdict(False, reasons + ["there is no GitHub PR for this ticket yet"])

    st = f.pr or {}
    if not st.get("ok"):
        reasons.append(f"could not read the PR from GitHub: {st.get('error', '')}")
    else:
        if st.get("merged"):
            reasons.append("the PR is already merged")
        elif st.get("state") != "OPEN":
            reasons.append(f"the PR is {st.get('state', 'not open').lower()}")
        if st.get("checks") == "pending":
            reasons.append("GitHub checks are still running")
        elif st.get("checks") == "failed":
            reasons.append("GitHub checks failed")
        elif st.get("checks") == "none":
            reasons.append("GitHub reports no checks on this PR")

    # The PR itself is what gets merged, so its own commits and head are what must be checked.
    if st.get("ok"):
        stray_on_pr = [s for s in st.get("commit_subjects", []) if f.key not in s]
        if stray_on_pr:
            reasons.append(f"the PR itself carries {len(stray_on_pr)} commit(s) that are not {f.key}, "
                           f"starting with: {stray_on_pr[0][:70]}")
        if f.branch_head is not None and st.get("head") and f.branch_head != st["head"]:
            reasons.append("the PR's head is not the branch's current commit; the PR needs updating first")

    if f.branch_head is None:
        reasons.append("the ticket's branch is missing")
    else:
        missing = REQUIRED_APPROVALS - f.approved_at_head
        if missing:
            reasons.append(f"not approved on the current commit by: {', '.join(sorted(missing))}")
        ok, stray = f.stray if f.stray is not None else (True, ())
        if not ok and stray and stray[0].startswith("could not"):
            reasons.append(stray[0])
        elif not ok:
            reasons.append(f"the PR would also add {len(stray)} commit(s) that are not {f.key}, "
                           f"starting with: {stray[0][:70]}")
    return Verdict(not reasons, reasons)
