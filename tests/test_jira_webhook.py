from wf import jira_webhook


def test_a_wrong_or_missing_token_is_refused():
    assert jira_webhook.token_ok("s3cret", "s3cret")
    assert not jira_webhook.token_ok("s3cret", "guess")
    assert not jira_webhook.token_ok("s3cret", None)
    assert not jira_webhook.token_ok("", "")  # no token configured: refuse everything


def test_only_ticket_and_sprint_changes_wake_the_sync():
    assert jira_webhook.is_relevant("jira:issue_updated")
    assert jira_webhook.is_relevant("sprint_started")
    assert not jira_webhook.is_relevant("comment_created")
    assert not jira_webhook.is_relevant(None)
