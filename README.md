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

New here? Run `wf init`. It asks you to connect the Atlassian MCP in Claude Code, then looks
at the git repos under the folders you name. It lists the ones you commit to most, with the
org/repo each pushes to, and you pick the ones to link.

```bash
wf init --root ~/code              # interactive
wf init --mcp-connected --root ~/code --pick 1,3   # scripted
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
wf status                          # when the board last synced, and what is blocked
wf move ABC-1 pr_open --actor dev --reason "opened PR"   # move a ticket to a mapped stage
wf flag ABC-1 needs-human --actor dev --reason "scope unclear"
wf flag ABC-1 needs-human --actor user --clear           # only the user clears a flag
```

To look at the board in a browser:

```bash
WF_DB=wf.sqlite WF_CONFIG=workflow.json python -m uvicorn --factory wf.entrypoints.web.app:create_app --port 8000
```

Then open http://127.0.0.1:8000/board. Pages: the active sprint, the backlog, each ticket,
and the Jira outbox.

`wf sync` takes the JSON payload that the Claude session builds from the Atlassian MCP.
See `docs/sync-format.md`.

## How it is built, and how to extend it

`wf` is built with four patterns, chosen so that you can change or add a part without touching the rest:

- **Hexagonal architecture (ports and adapters).** The core knows nothing about SQLite, git, GitHub, Jira, or Claude.
  It talks to the outside through *ports* (small `typing.Protocol`s in `src/wf/application/ports`), and each outside
  system is an *adapter* that implements one (`src/wf/adapters`). The CLI, the web app, and the agent gateway are
  *entrypoints* that call the core (`src/wf/entrypoints`).
- **Domain-driven design.** The domain is split into bounded contexts (board, delivery, execution, tracker, workspace)
  that share a small vocabulary (`Stage`, `Role`, `Actor`, `TicketKey`). Rules are pure functions and aggregates in
  `src/wf/domain`, with no I/O. A `Ticket` records typed domain events for every change.
- **Inversion of control.** The core never creates an adapter. Frameworks (FastAPI, the CLI parser, the MCP loop) call
  into use cases, and use cases call the ports they were given.
- **Dependency injection.** Use cases take their ports as constructor arguments. One module, `src/wf/bootstrap.py`,
  decides which adapter fills each port. There is no DI library; the wiring is plain code.

Dependencies point inward: `entrypoints` and `adapters` depend on `application`, which depends on `domain`.
`tests/test_architecture.py` fails if an import breaks that, so the structure stays intact as the project grows.

Where to start when you want to:

| You want to | Do this |
|---|---|
| Change a rule (who may move a ticket, when a PR is good to merge) | Edit the pure function in the right `src/wf/domain/<context>/` package and its unit test. |
| Add a behavior | Add a use case class in `src/wf/application/<context>/` that takes ports. Test it with the in-memory fakes in `tests/fakes.py`. |
| Support another system (another tracker, forge, model, or database) | Add a port in `src/wf/application/ports/` if none fits, write an adapter in `src/wf/adapters/`, add a contract test in `tests/contract/`, and wire it in `bootstrap.py`. |
| Add a screen | Add a view model in `src/wf/entrypoints/web/viewmodels/`, a template bound to it, and a route that renders it. |
| Add a command | Add it to `src/wf/entrypoints/cli.py`; it asks the container for use cases. |

Read `docs/architecture.md` for the layers, the domain model, the ports, and the testing approach.

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
| `docs/architecture.md` | The layers, ports, and how to extend them. Start here. |
| `docs/plan.md` | Product decisions and the history of how the flow was built. |
| `docs/mac-setup.md` | Setup for the Claude Code flow on Apple Silicon Macs. |
| `prompts/` | The system prompt for each agent role. Project rules come from configuration. |
| `src/wf/domain/` | The model and the rules: pure code, no I/O. |
| `src/wf/application/` | Use cases, and the ports they depend on. |
| `src/wf/adapters/` | SQLite, git, forges, Jira, models, runners, config files. |
| `src/wf/entrypoints/` | The CLI, the web app, and the MCP gateway. |
| `src/wf/bootstrap.py` | The composition root: wires adapters to ports. |
| `tests/` | The test suite, including the architecture rules in `test_architecture.py`. |

## License

MIT. See `LICENSE`.
