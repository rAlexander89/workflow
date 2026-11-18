# Architecture

`wf` is a hexagonal application: the rules and use cases sit in the middle and know nothing about
SQLite, git, GitHub, Jira, Claude Code, or HTTP. Those are adapters, plugged in through ports.
Dependencies point inward, and one module wires everything together.

```
 entrypoints (CLI, web, MCP gateway, webhooks)         driving adapters: call use cases
        │
        ▼
   application   ── use cases, and the ports they need ──►  ports (protocols)
        │                                                        ▲
        ▼                                                        │ implement
     domain      pure rules and the model                   adapters (SQLite, git, forges,
                                                            Jira session, models, runners, files)

 bootstrap.py    the composition root: the only module that knows which adapter implements which port
```

## The layers

| Layer | Holds | May import |
|---|---|---|
| `wf/domain` | The model and the rules, as pure code: no I/O, clock, or environment | `wf.domain` and the standard library |
| `wf/application` | Use cases, and the ports (protocols) they call out through | `wf.domain`, `wf.application` |
| `wf/adapters` | One implementation per port: SQLite, git, forges, the Jira session, models, runners, config files | `wf.domain`, `wf.application`, `wf.adapters` |
| `wf/entrypoints` | The CLI, the web app, the MCP gateway. They translate input into use case calls | `wf.domain`, `wf.application`, `wf.bootstrap`, `wf.entrypoints` |
| `wf/bootstrap.py` | `Container` and `Session`: builds adapters, hands use cases to entrypoints | everything |

`tests/test_architecture.py` enforces this by parsing imports. Domain and application also may not import an I/O
library (`sqlite3`, `subprocess`, `httpx`, `fastapi`, `urllib.request`, ...) or read the clock or environment
(`time.time`, `os.environ`).

## Bounded contexts

Each context has its own package under `domain/` and use cases under `application/`.

| Context | Owns |
|---|---|
| **board** | Ticket (the aggregate), stages, who may move a ticket, flags, approvals, blocks, the Jira status mapping, the sync payload |
| **delivery** | Stage owners and routing, the pipeline's decisions (advance, plan a pass, why a ticket is idle), the merge gate and merge bot, pickup, deploys, publishing |
| **execution** | An agent run: its brief, gateway environment, and result; model usage; the model vocabulary; the gateway's rules |
| **tracker** | Jira status changes, the changelog, when to poll, the outbox of moves waiting for Jira |
| **workspace** | Which repos and boards are linked, and how the harness is configured |

## Facts in, decision out

Rules that used to mix SQL with logic are split in two. A use case gathers a snapshot of facts through ports, calls a
pure function in the domain, and acts on the answer:

- `PipelineService.advance` gathers `AdvanceFacts`, `domain.delivery.pipeline.advance_move` decides.
- `PipelineService.plan_pass` gathers `TicketFacts`, `plan_pass` decides who runs and who waits.
- `PipelineService.idle_reason` gathers `IdleFacts`, `idle_reason` explains why a ticket is not moving.
- `MergeBot.assess` gathers `PullRequestFacts`, `merge_gate.verdict` lists why a PR is not good to merge.

A rule change is a change to one small pure function with a unit test that needs no database.

## Ports

Defined in `wf/application/ports/`, as `typing.Protocol`s:

| Port | Adapter |
|---|---|
| `TicketRepository`, `StageMappings`, `OutboxQueue`, `BoardUnitOfWork` | SQLite (`adapters/persistence/sqlite/board.py`) |
| `BoardQueries`, `BoardSync` | SQLite (`board_queries.py`, `board_sync.py`) |
| `PauseState`, `AgentRunRepository`, `RunRegistry`, `UsageLog`, `ChangeLog`, `LifecycleLog` | SQLite |
| `Workspace`, `TestRunner` | git worktrees and a shell (`adapters/workspace/git.py`) |
| `Forge`, `PullRequests` | GitHub (`gh`) and GitLab (`glab`) (`adapters/forge/`) |
| `Tracker`, `IssueReader` | the user's Claude session with the Jira connector; the Jira REST API (`adapters/tracker/`) |
| `AgentRunner`, `PromptLibrary`, `Toolbox`, `AppSession` | Claude Code CLI, the prompts folder, the worktree and the local app (`adapters/runner/`) |
| `ModelPort` | Ollama, the Claude API, Claude Code (`adapters/model/`) |
| `WorkflowEngine` | LangGraph, checkpointed in the board's SQLite file (`adapters/workflow/`) |
| `ActivityLog`, `TaskRunner` | the web app's activity log and background thread (`entrypoints/web/activity.py`) |

## Dependency injection and inversion of control

Use cases take their ports in their constructors, as keyword arguments. Nothing in `domain` or `application`
constructs an adapter. A framework calls into the use cases (FastAPI routes, the CLI parser, the MCP loop), not the
other way round.

`wf.bootstrap.Container` is built once per process with the paths it needs. `container.session()` opens one board
connection and yields a `Session` with the use cases built over it: `session.tickets`, `session.pipeline(settings)`,
`session.merge(settings)`, and so on. There is no DI library; the wiring is plain code you can read in one file.

Long actions run on a worker thread, so each one opens its own session (see `Container.background_work`).

## Testing

- **Domain:** plain unit tests with no fixtures.
- **Application:** in-memory fakes of the ports (`tests/fakes.py`).
- **Adapters:** contract tests (`tests/contract/`) run each port against the SQLite adapter and the in-memory fake, so
  the fakes stay faithful. `tests/test_ports_conformance.py` checks every adapter and fake has each method of its
  port, with the same parameter names.
- **Entrypoints:** FastAPI's `TestClient` and the CLI's `main` over a real temporary database.
- `tests/support/` holds connection-based helpers (`board.move_stage(conn, ...)`, `agents.tick(conn, ...)`) that the
  older integration tests use. They build the same use cases the container does.

## Adding things

- **A rule:** a pure function in the right `domain/` package, with a unit test. Call it from a use case.
- **A use case:** a class in `application/<context>/`, taking ports. Test it with fakes.
- **A new external system:** a protocol in `application/ports/`, an adapter in `adapters/`, a contract test, and a line
  in `bootstrap.py`.
- **A screen or command:** an entrypoint that asks the container for a session and calls use cases.

## Decisions

- Hand-written composition root rather than a DI library: fewer dependencies for a public tool, and the graph is small.
- Subpackages by layer (`domain/`, `application/`, ...) so the import rules can be checked mechanically.
- LangGraph stays, behind `WorkflowEngine`, compiled with its checkpointer from the board's database. It is used only
  by `wf run` and the Runs pages.
- Domain events are not used yet. A ticket's history is an append-only list of `TicketEvent`s, and the pipeline's
  questions (approvals, perf requests, "changed since") are pure functions over it. Publishing those events to
  subscribers is the next step if the history, the outbox, and the live stream should react to the same change.
- The SQLite schema did not change. Existing `wf.sqlite` files keep working.
