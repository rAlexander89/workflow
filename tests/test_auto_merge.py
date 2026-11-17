import subprocess

from wf import board, github_agent
from tests.test_board import clock, payload, sync, ticket

ISSUE = "ABC-29"


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


def setup(tmp_path, stage="qa_approved"):
    c = board.connect(tmp_path / "wf.sqlite")
    sync(c, payload(tickets=[ticket(f"{ISSUE}", status="Ready for dev")]))
    c.execute("update ticket set stage = ? where key = ?", (stage, ISSUE))
    c.execute("insert into ticket_event (ticket_key, at, actor, kind, from_value, to_value, detail) "
              "values (?, 1, 'harness', 'pr', null, null, 'https://github.com/o/r/pull/9')", (ISSUE,))
    board.record_approval(c, ISSUE, "pr_review", "abc123", clock=clock)
    board.record_approval(c, ISSUE, "perf_review", "abc123", clock=clock)
    board.record_approval(c, ISSUE, "qa", "abc123", clock=clock)
    c.commit()
    return c


def green(**kw):
    base = {"ok": True, "state": "OPEN", "merged": False, "checks": "passed"}
    base.update(kw)
    return lambda url: base


def test_merges_when_qa_passed_checks_green_and_nothing_flagged(tmp_path):
    c = setup(tmp_path)
    git = Git()
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=git, status=green()) == [ISSUE]
    assert ["gh", "pr", "merge", "https://github.com/o/r/pull/9", "--merge"] in git.calls
    assert c.execute("select stage from ticket where key = ?", (ISSUE,)).fetchone()[0] == "merged"


def test_a_flag_stops_the_merge(tmp_path):
    c = setup(tmp_path)
    board.set_flag(c, ISSUE, "needs-human", "pr_review", "a real issue", clock=clock)
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=Git(), status=green()) == []


def test_failing_checks_stop_the_merge(tmp_path):
    c = setup(tmp_path)
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=Git(), status=green(checks="failed")) == []


def test_without_qa_approval_on_this_commit_nothing_merges(tmp_path):
    c = setup(tmp_path)
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=Git(head="other"), status=green()) == []


def test_a_branch_that_adds_other_commits_is_never_merged(tmp_path):
    c = setup(tmp_path)
    git = Git(subjects="ABC-29: fix\nSend each listing once")
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=git, status=green()) == []
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in git.calls)


def test_a_missing_code_performance_review_stops_the_merge(tmp_path):
    c = setup(tmp_path)
    c.execute("delete from ticket_event where ticket_key = ? and kind = 'approve' and actor = 'perf_review'", (ISSUE,))
    c.commit()
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=Git(), status=green()) == []


def test_the_merge_bot_reads_the_pr_itself_not_the_local_branch(tmp_path):
    c = setup(tmp_path)
    # the local branch is clean, but the PR on GitHub carries another ticket's commits
    status = lambda url: {"ok": True, "state": "OPEN", "merged": False, "checks": "passed",
                          "head": "abc123", "commit_subjects": ["Merge branch 'main' of github", "ABC-29: fix"]}
    git = Git(subjects="ABC-29: fix login heading")
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=git, status=status) == []
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in git.calls)


def test_a_pr_whose_head_is_not_the_branch_head_is_not_merged(tmp_path):
    c = setup(tmp_path)
    status = lambda url: {"ok": True, "state": "OPEN", "merged": False, "checks": "passed",
                          "head": "other", "commit_subjects": ["ABC-29: fix"]}
    assert github_agent.merge_when_clear(c, repo=tmp_path, runner=Git(), status=status) == []
