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

## Domain model (DDD)

The domain is split into bounded contexts plus a small shared kernel. A context stands alone: it imports the
shared kernel and itself, never another context. `tests/test_architecture.py` enforces that.

### Ubiquitous language (shared kernel, `wf/domain/shared`)

| Term | Meaning |
|---|---|
| **Stage** (`Stage`) | A step in the internal workflow: backlog, in_refinement, ready_for_dev, in_dev, pr_open, ..., deployed. A Jira status maps to at most one stage. |
| **Role** (`Role`) | An agent role: product, staff, dev, oncall, pr_review, perf_review, qa, performance. |
| **Actor** (`Actor`) | Who leaves a mark on a ticket that is not a role: user, harness, sync, outbox, jira-agent, observer, merge_bot. |
| **Ticket key** (`TicketKey`) | A validated `PROJECT-123`. |
| **Commit** (`CommitSha`) | A git commit. An approval covers exactly one. |
| **Sprint state** (`SprintState`) | future, active, closed. Only an active sprint's tickets are worked. |

### Contexts

| Context | Aggregates and entities | Value objects and events | Domain services (pure rules) |
|---|---|---|---|
| **board** (core) | `Ticket` (aggregate root: stage, flags, order, Jira status, history) | `TicketEvent`; domain events `TicketMoved`, `TicketFlagged`, `FlagCleared`, `TicketCommented`, `ApprovalRecorded`, `PerfCheckRequested`, `TicketReordered`, `TicketBlocked`, `SentToJira`; `Flag` | who may move a ticket (`moves`), Jira status to stage mapping (`stages`), the sync payload's shape (`payload`), questions about history (`events`) |
| **delivery** | | `Move`, `AdvanceFacts`, `TicketFacts`, `IdleFacts`, `PullRequestFacts`, `Verdict`, `Owner` | pipeline decisions (`pipeline`), routing, the merge gate and the merge bot's verdict (`merge_gate`), pickup, deploys and publishing, roles and limits (`roles`), CI check summaries |
| **execution** | `AgentRunRecord` (one run of one role on a ticket) | `AgentResult`, `AgentRunRequest`, `RunResult`, `UsageRecord`, model request and response | reading a ticket's runs: whose turn, when to stop (`run_history`); the brief, system prompt, and gateway environment (`agent_run`); the gateway's rules (`gateway_rules`) |
| **tracker** | | `StatusChange`, `Observation`, `OutboxDecision`, `OutboxStatus` | parsing a changelog and deciding when to poll (`changelog`); what to do with each queued Jira move (`outbox_rules`); webhook tokens |
| **workspace** | `Config` (repos, boards, model, agents, bots) | `RepoLink`, `BoardLink`, `ModelConfig`, `AgentsConfig`, `BotsConfig` | validation of what may be linked |

### Domain events

A `Ticket` records a typed event for every change it accepts and hands them over with `pull_events()`. The repository
writes each event's history line (`event.record()`) and returns the events. `TicketCommands` publishes them on an
in-process `EventBus` inside the same unit of work, so a handler's writes commit or roll back with the change that
caused them. Subscriptions are wired in `bootstrap.build_event_bus()`. Today: a `TicketMoved` queues the matching
move in Jira (`application/tracker/handlers.py`).

### Context map

```
 shared kernel  <──  board  (core)
       ▲               ▲
       │               │ use cases combine them; the domains do not import each other
       ├──────  delivery, execution, tracker, workspace
```

Contexts meet in the application layer: a use case reads one context's facts and applies another's rules. For example
`PipelineService` reads a ticket's history (board) and its runs (execution), and asks `delivery.pipeline` what to do.

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

## Views: MVVM

The web UI follows Model-View-ViewModel.

- **Model:** the application (use cases) and the domain.
- **View:** a Jinja template in `entrypoints/web/templates`. It reads only its view model and has no logic beyond
  presentation. No HTML is built in Python.
- **ViewModel:** a class in `entrypoints/web/viewmodels`, one per screen. It asks the container's use cases for what the
  screen shows (`load`), shapes it for display, and offers the actions a person can take (`send_back`, `answer`,
  `PipelineCommands.merge`, ...). A template is bound to its view model: each public field is a name in the template,
  and `vm` is the view model itself.
- **Routes** (`app.py`, `board_views.py`, `agents_views.py`) only create a view model and render it, or call a command.
  They never open a board session or read settings; `test_architecture.py` checks that.

| Screen | View model |
|---|---|
| Pipeline page and its fragments | `PipelineViewModel`, `PipelineBoardViewModel`, `ColumnViewModel`, `CardViewModel`, `ActivityViewModel`, `TicketLiveViewModel`, `PipelineCommands` |
| Ticket page | `TicketViewModel` |
| PR card | `PrCardViewModel` |
| Board, backlog, item, outbox, deploy panel | `BoardViewModel`, `BacklogViewModel`, `ItemViewModel`, `OutboxViewModel`, `DeployPanelViewModel` |
| Links and runs | `LinksViewModel`, `LinksPanelViewModel`, `CheckViewModel`, `RunsViewModel`, `RunViewModel`, `RunStatusViewModel` |

The live stream (`/events`) renders fragments from the same view models, so the page and the stream cannot disagree.

## Testing

Tests are organized by layer (`tests/domain`, `application`, `adapters`, `entrypoints`, `integration`, `contract`):

- **Domain:** plain unit tests with no fixtures.
- **Application:** use cases with in-memory fakes of the ports (`tests/fakes.py`), or over SQLite through the fixtures in
  `tests/conftest.py` (`tickets`, `queries`, `board_sync`, `outbox_service`, `session`).
- **Adapters:** each adapter against the real thing (SQLite, git). `tests/contract` runs each port against the SQLite
  adapter and the in-memory fake, so the fakes stay faithful, and `tests/adapters/test_ports_conformance.py` checks every
  adapter and fake has each method of its port. `BoardQueries` and `BoardSync` are SQLite-only read and bulk-import
  models, so they are tested through the adapter and have no in-memory fake.
- **Entrypoints:** FastAPI's `TestClient`, the CLI's `main`, and the gateway, over a real temporary database. View models
  are tested directly (`tests/entrypoints/test_viewmodels.py`).
- **Integration:** `tests/integration` runs the pipeline over real SQLite and git, with the outside world faked.
- **Seams:** the container takes `Overrides` (agent runner, forge, pull requests, tracker, git runner), so a test gives
  fakes for what reaches outside the machine and uses everything else as it is. `tests/builders.py` has the test data
  (Jira payloads, git repos, a container for a test).

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
- Domain events are published in process, inside the unit of work (see above). There is no broker: nothing here needs one.
- The SQLite schema did not change. Existing `wf.sqlite` files keep working.
