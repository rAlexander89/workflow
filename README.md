# wf

A local multi-agent workflow for software teams. Agents work on tickets from your
issue tracker in an internal sprint board, with a person making the decisions that
matter.

## What it does

- **Reads your tickets from Jira**, using the Atlassian MCP connection in your Claude
  session. The tool discovers your projects, boards, and workflow statuses at startup.
- **Maps your statuses to internal stages.** Nothing is hard-coded. Each team's
  statuses come from its own Jira projects.
- **Runs the work locally.** Agents work on branches in internal git repositories.
  They open internal pull requests, which a review agent and a QA agent check. Nothing
  is pushed to your organization's hosting until those checks pass.
- **Records everything in SQLite**: the ticket lifecycle, runs, and token use.
- **Uses the model you choose.** Supported providers are a local model through Ollama,
  the Claude API with an API key, and the Claude Code CLI under your own subscription
  login (for operator-provisioned machines only).

## Requirements

- Python 3.11 or newer
- git
- The Atlassian MCP connected in your Claude session (required for discovery)
- For the Claude Code provider: Claude Code, signed in with your subscription

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

Commands that exist now:

```bash
wf link repo api /srv/wf/api.git   # link an internal repo
wf link board jira 12              # link a Jira board
wf list                            # show linked repos and boards
wf check                           # verify each link resolves
wf doctor                          # check this machine for the Claude Code flow
wf model status                    # show the model provider and access
wf sync payload.json               # apply a sprint payload from the Claude session
wf board                           # the active sprint, grouped by stage
wf backlog                         # your backlog (tickets in no sprint), grouped by stage
wf sprints --state active          # list sprints
wf route "Ready for dev"           # which owner a Jira status routes to
```

`wf sync` takes the JSON payload that the Claude session builds from the Atlassian MCP.
See `docs/sync-format.md`.

## Configuration

`workflow.json` holds identifiers only: linked repos and boards, and model settings.
Credentials are never written to it.

- **Jira:** `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` in the environment, for
  link checks.
- **Model:** the provider is set with `wf model set`. The Anthropic API key is read
  from the variable named in the config, never stored in it.

## Layout

| Path | What's there |
|---|---|
| `docs/plan.md` | Architecture and decisions. Start here. |
| `docs/mac-setup.md` | Setup for the Claude Code flow on Apple Silicon Macs. |
| `prompts/` | The system prompt for each agent role. Project rules come from configuration. |
| `src/wf/` | Config, Jira client, event intake, supervisor, model adapters, runs, and the CLI. |
| `src/wf/web/` | A small web app for links and runs. |
| `tests/` | The test suite. |

## License

MIT. See `LICENSE`.
