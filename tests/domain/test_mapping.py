from wf.domain.board.stages import map_project, normalize, stages_for


def test_common_status_names_match_one_stage():
    cases = {
        "Backlog": "backlog",
        "In refinement": "in_refinement",
        "Hotfix review": "hotfix_review",
        "Ready for dev": "ready_for_dev",
        "Ready for development": "ready_for_dev",
        "Dev in progress": "in_dev",
        "In Progress": "in_dev",
        "PR open": "pr_open",
        "PR in review": "pr_in_review",
        "In review": "pr_in_review",
        "Ready for QA": "ready_for_qa",
        "QA approved": "qa_approved",
        "Merged to main": "merged",
        "Regression testing": "regression",
        "Deployed to dev": "deployed",
    }
    for name, stage in cases.items():
        assert stages_for(name) == [stage], name


def test_normalize_ignores_case_and_punctuation():
    assert normalize("Ready-for-dev!") == "ready for dev"
    assert normalize("  PR   in   review ") == "pr in review"


def test_a_status_matching_two_stages_is_left_out_and_reported():
    rows, problems = map_project(["Ready for dev in review"])
    by_stage = {r.stage: r for r in rows}
    assert by_stage["ready_for_dev"].status_name is None
    assert by_stage["pr_in_review"].status_name is None
    assert any("several stages" in p for p in problems)


def test_two_statuses_for_one_stage_are_blocked_as_ambiguous():
    rows, _ = map_project(["Ready for dev", "Ready for development"])
    row = next(r for r in rows if r.stage == "ready_for_dev")
    assert row.status_name is None and "ambiguous" in row.reason


def test_a_stage_with_no_status_is_unmapped():
    rows, _ = map_project(["Backlog"])
    row = next(r for r in rows if r.stage == "qa_approved")
    assert row.status_name is None and row.reason == "no status matches this stage"


def test_one_status_per_stage_is_mapped():
    rows, problems = map_project(["Backlog", "Ready for dev", "Dev in progress"])
    mapped = {r.stage: r.status_name for r in rows if r.status_name}
    assert mapped == {"backlog": "Backlog", "ready_for_dev": "Ready for dev", "in_dev": "Dev in progress"}
    assert problems == []


def test_open_and_to_do_are_backlog_statuses():
    assert stages_for("Open") == ["backlog"]
    assert stages_for("To Do") == ["backlog"]
    assert stages_for("PR open") == ["pr_open"]
