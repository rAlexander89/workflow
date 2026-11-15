# Sync payload

The Atlassian MCP runs inside the user's Claude session, so the session does the Jira
queries and passes the results to the tool as one JSON file:

```bash
wf sync payload.json --db wf.sqlite
```

Jira is read-only. The payload is never written back.

## Shape

```json
{
  "kind": "active",
  "projects": [
    {
      "key": "ABC",
      "name": "Example",
      "statuses": [
        {"name": "Ready for dev", "category": "new", "source": "observed"},
        {"name": "Dev in progress", "category": "indeterminate", "source": "observed"}
      ]
    }
  ],
  "sprints": [
    {"id": "101", "board_id": "7", "name": "Sprint 4", "state": "active",
     "goal": null, "start_date": 1763000000, "end_date": 1763600000, "complete_date": null}
  ],
  "tickets": [
    {"key": "ABC-1", "project_key": "ABC", "jira_id": "10001", "summary": "Add search",
     "description": "...", "acceptance": "...", "issue_type": "Story", "priority": "High",
     "points": null, "assignee_id": "712020:abc", "status": "Dev in progress",
     "status_category": "indeterminate", "labels": ["api"], "sprint_id": "101",
     "updated": 1763100000}
  ],
  "comments": [
    {"ticket_key": "ABC-1", "jira_comment_id": "20001", "author": "Ana",
     "body": "Please keep the old endpoint.", "created": 1763050000, "updated": null}
  ]
}
```

- `kind` is required. `active` covers the signed-in user's tickets in active sprints.
  `past` covers tickets fetched on demand from closed sprints. `backlog` covers the
  signed-in user's tickets in no sprint that aren't done.
- `projects`, `sprints`, `tickets`, and `comments` are optional lists.
- Dates and times are Unix seconds.
- Statuses come from the project's statuses. Include a status with `"source": "observed"`
  when it came from a ticket, and `"workflow"` when it came from the project's workflow.
- Ticket `sprint_id` is the sprint the ticket is in now. A ticket belongs to one sprint.

## The queries the session runs

| Payload part | Atlassian MCP call |
|---|---|
| Account | `atlassianUserInfo` |
| Site | `getAccessibleAtlassianResources` (use the site's `id` as `cloudId`) |
| Projects | `getVisibleJiraProjects` |
| Statuses per project | `searchJiraIssuesUsingJql` with `project = KEY`, fields `status`, collecting the distinct statuses seen. Statuses with no tickets are not visible this way. |
| Active tickets | `searchJiraIssuesUsingJql` with `assignee = currentUser() AND sprint in openSprints()` |
| Past tickets | `searchJiraIssuesUsingJql` with `assignee = currentUser() AND sprint = <id>` |
| Backlog tickets | `searchJiraIssuesUsingJql` with `assignee = currentUser() AND sprint is EMPTY AND statusCategory != Done` (`kind: "backlog"`) |
| Comments | `searchJiraIssuesUsingJql` with the `comment` field, or the comment tool for each ticket |

## What the tool does with it

- **Mapping:** statuses are matched to internal stages by name. Stages without exactly one
  matching status are reported as blocked. See `src/wf/mapping.py`.
- **Active sync:** tickets missing from the payload, or moved out of an active sprint, are
  marked removed. Their rows and history are kept.
- **Past sync:** never overwrites a ticket whose current sprint is active.
- **Failures:** the sync is all-or-nothing. A failure rolls back every change and records
  the failed run in `sync_run`.
