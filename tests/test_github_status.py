import json
import subprocess

from wf import github_status as gs


def fake(payload, code=0):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, code, stdout=json.dumps(payload), stderr="boom")
    return run


def test_open_pr_with_checks_running_is_pending():
    st = gs.pr_status("u1", runner=fake({"state": "OPEN", "mergedAt": None, "mergeStateStatus": "UNSTABLE",
                                         "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]}), now=lambda: 1)
    assert st["ok"] and not st["merged"] and st["checks"] == "pending"


def test_a_failed_check_is_failed():
    st = gs.pr_status("u2", runner=fake({"state": "OPEN", "mergedAt": None,
                                         "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}), now=lambda: 2)
    assert st["checks"] == "failed"


def test_merged_pr_reports_merged_and_passed_checks():
    st = gs.pr_status("u3", runner=fake({"state": "MERGED", "mergedAt": "2026-11-16T20:00:00Z",
                                         "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}), now=lambda: 3)
    assert st["merged"] and st["checks"] == "passed"


def test_gh_failure_is_reported_not_raised():
    st = gs.pr_status("u4", runner=fake({}, code=1), now=lambda: 4)
    assert st["ok"] is False and "boom" in st["error"]
