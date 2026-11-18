"""Merging a PR when the merge bot says it is good, through the publish service with a fake git and gh."""
import subprocess

import pytest

from tests.builders import make_container, payload, sync, ticket
from wf.bootstrap import Overrides, Session

ISSUE = "ABC-29"
PR = "https://github.com/o/r/pull/9"


class Git:
    """Fakes the git and gh calls the merge needs."""
    def __init__(self, subjects="ABC-29: fix login heading", head="abc123", merge_ok=True):
        self.subjects, self.head, self.merge_ok, self.calls = subjects, head, merge_ok, []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        out = ""
        if argv[:2] == ["git", "log"]:
            out = self.subjects
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            out = self.head
        code = 0 if (argv[:2] != ["gh", "pr"] or self.merge_ok) else 1
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


class PullRequests:
    def __init__(self, status):
        self._status = status

    def status(self, url):
        return self._status(url)


@pytest.fixture
def merge_when_clear(tmp_path, repo, conn):
    """Run the merge pass with the given fake git and PR status, over a QA-approved ticket with a PR."""
    sync(conn, payload(tickets=[ticket(ISSUE, status="Ready for dev")]))
    conn.execute("update ticket set stage = 'qa_approved' where key = ?", (ISSUE,))
    conn.commit()

    def run(git, status, prepare=None):
        container = make_container(tmp_path, repo=repo, overrides=Overrides(
            git_runner=git, pull_requests=PullRequests(status)))
        session = Session(container, conn)
        session.tickets.record_note(ISSUE, "harness", "pr", PR)
        for role in ("pr_review", "perf_review", "qa"):
            session.tickets.record_approval(ISSUE, role, "abc123")
        if prepare:
            prepare(session)
        return session.publish(container.settings()).merge_when_clear()
    return run


def green(**kw):
    base = {"ok": True, "state": "OPEN", "merged": False, "checks": "passed"}
    base.update(kw)
    return lambda url: base


def test_merges_when_qa_passed_checks_green_and_nothing_flagged(merge_when_clear, conn):
    git = Git()
    assert merge_when_clear(git, green()) == [ISSUE]
    assert ["gh", "pr", "merge", PR, "--merge"] in git.calls
    assert conn.execute("select stage from ticket where key = ?", (ISSUE,)).fetchone()[0] == "merged"


def test_a_flag_stops_the_merge(merge_when_clear):
    flag = lambda session: session.tickets.set_flag(ISSUE, "needs-human", "pr_review", "a real issue")  # noqa: E731
    assert merge_when_clear(Git(), green(), prepare=flag) == []


def test_failing_checks_stop_the_merge(merge_when_clear):
    assert merge_when_clear(Git(), green(checks="failed")) == []


def test_without_qa_approval_on_this_commit_nothing_merges(merge_when_clear):
    assert merge_when_clear(Git(head="other"), green()) == []


def test_a_branch_that_adds_other_commits_is_never_merged(merge_when_clear):
    git = Git(subjects="ABC-29: fix\nSend each listing once")
    assert merge_when_clear(git, green()) == []
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in git.calls)


def test_a_missing_code_performance_review_stops_the_merge(merge_when_clear, conn):
    def drop(session):
        conn.execute("delete from ticket_event where ticket_key = ? and kind = 'approve' and actor = 'perf_review'",
                     (ISSUE,))
        conn.commit()
    assert merge_when_clear(Git(), green(), prepare=drop) == []


def test_the_merge_bot_reads_the_pr_itself_not_the_local_branch(merge_when_clear):
    # the local branch is clean, but the PR on GitHub carries another ticket's commits
    status = lambda url: {"ok": True, "state": "OPEN", "merged": False, "checks": "passed",
                          "head": "abc123", "commit_subjects": ["Merge branch 'main' of github", "ABC-29: fix"]}
    git = Git(subjects="ABC-29: fix login heading")
    assert merge_when_clear(git, status) == []
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in git.calls)


def test_a_pr_whose_head_is_not_the_branch_head_is_not_merged(merge_when_clear):
    status = lambda url: {"ok": True, "state": "OPEN", "merged": False, "checks": "passed",
                          "head": "other", "commit_subjects": ["ABC-29: fix"]}
    assert merge_when_clear(Git(), status) == []
