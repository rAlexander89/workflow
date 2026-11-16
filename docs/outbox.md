# The Jira outbox

When a ticket moves to a stage that has a mapped Jira status, the move is queued in the
`outbox` table. The Jira agent sends queued moves to the real Jira board. It runs in the
user's Claude session, because the Atlassian MCP lives there. Python decides what to send;
the session only calls Jira.

## The session's steps

1. **List what's waiting.**
   ```bash
   wf outbox pending --db wf.sqlite
   ```
   This prints the queued moves (JSON). Each one has a `ticket_key`.

2. **Read Jira for each ticket** through the MCP:
   - `getJiraIssue` with `fields: ["status"]`, for the current status name.
   - `getTransitionsForJiraIssue`, for the transitions Jira allows from that status.

   Write the results to `observations.json`:
   ```json
   {"observations": {
     "ABC-1": {"status": "Ready for dev",
               "transitions": [{"id": "31", "to": "Dev in progress"}]}
   }}
   ```
   `to` is the status name the transition leads to. A ticket the session couldn't read
   can be left out.

3. **Plan.**
   ```bash
   wf outbox plan observations.json --db wf.sqlite > plan.json
   ```
   The result lists `send` items (each with a `transition_id`), and `need` for tickets
   that weren't in the observations. Anything else is decided already:
   - **skipped**: Jira is already in the target status.
   - **conflict**: Jira's status changed since our last sync. The move is not forced, and
     the ticket is flagged `needs-human`.
   - **blocked**: Jira has no transition to the target from its current status.

4. **Send.** For each item in `send`, call `transitionJiraIssue` through the MCP with the
   ticket key and the `transition_id`. Record the result for each one in `results.json`:
   ```json
   [{"id": 7, "ok": true, "jira_status": "Dev in progress", "response": {"status": 204}},
    {"id": 8, "ok": false, "error": "HTTP 400: transition not allowed"}]
   ```

5. **Report.**
   ```bash
   wf outbox ack results.json --db wf.sqlite
   ```

## Rules the outbox enforces

- A queued move is sent at most once. Once an item is `sending`, planning skips it, and
  only a failed or stuck item can be put back with `wf outbox retry ID`.
- A newer move for the same ticket supersedes older queued moves, so Jira never gets a
  stale move.
- The Jira agent only moves tickets assigned to the user. Those are the only tickets the
  board holds.
- Sync never queues anything back to Jira. Only moves made on the board do.

## Not yet

- Comments and field updates. The decision is "any interaction with tickets assigned to
  me", so these come next.
- Running the session steps on a schedule.
