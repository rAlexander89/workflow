# TODO

## Jira webhook (receiver is built; not live)
- [ ] Create the token file `~/.config/wf/jira-webhook.env` with `WF_JIRA_WEBHOOK_TOKEN=<random>` (owner-only, chmod 600).
- [ ] Start the tunnel: `ngrok http 8001` (needs approval; blocked in the last session).
- [ ] Restart the server with the token set, so the receiver accepts requests.
- [ ] Register the webhook in Jira: Settings > System > WebHooks (needs a Jira admin), or an automation rule if not admin.
      URL: `https://<tunnel>/webhooks/jira/<token>`. Events: issue created/updated/deleted, sprint created/started/updated/closed.
- [ ] Test: change a FKST ticket and confirm the sync log line appears.

## Sync
- [x] Sync reads the user's tickets in open sprints and backlog tickets (no sprint, not Done).
- [ ] Keep manual sync for now. Revisit a timer once the webhook is live.

## GitHub webhook
- [ ] Same as Jira: create `~/.config/wf/github-webhook.env`, start the tunnel, register on rAlexander89/fokist.
