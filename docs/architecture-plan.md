# Plan: hexagonal architecture, IoC, DI, DDD

Status: proposal, nothing built. Behavior stays the same throughout. The SQLite schema does
not change, so the existing `wf.sqlite` keeps working.

## Why

Today most modules take a `sqlite3.Connection` and call SQLite, `subprocess` (git, `gh`,
`glab`, `claude`), or HTTP directly, next to the rules they enforce. A rule such as "when may
the harness advance a ticket" cannot be tested or changed without a database and a git repo.
Adapters (Jira, GitHub, GitLab, Claude Code, Ollama, SQLite) cannot be swapped or extended in
isolation. The goal: rules live in a core that imports no adapter, and every external system
sits behind a port that is injected.

## Principles

- **Hexagonal.** The core defines ports. Adapters implement them. Dependencies point inward.
- **IoC.** The core never constructs an adapter. A framework (FastAPI, the CLI, the scheduler)
  calls into use cases, and use cases call ports they were given.
- **DI.** Constructor injection of small protocols. One composition root builds the object
  graph. Hand-written, no DI framework (fewer dependencies for a public tool). FastAPI gets
  use cases through `Depends`, so tests override them with fakes.
- **DDD.** Domain model organized by bounded context, in the language the team already uses
  (ticket, stage, role, flag, approval, block, run). Rules are pure functions or methods on
  entities and value objects. No I/O, no clock, no `sqlite3`.

## Bounded contexts (from the current code)

| Context | Owns | Today in |
|---|---|---|
| **Board** | Sprint, Ticket (aggregate root), Comment, Flag, Block (prerequisite link), Approval (role + head commit), StageMapping | `board.py`, `mapping.py` |
| **Delivery** | Stage ownership and routing, pipeline advancement, refinement rounds, idle reasons, pause, merge gate, publish gate | `orchestrator.py`, `agents.py` (advance_pipeline, idle_reason, refinement_role), `pause.py`, `merge.py`, `pickup.py`, `github_agent.py`, `merge_bot.py` |
| **Execution** | Run, AgentResult, role and prompt, worktree, usage ledger, local app environment | `agents.py` (run_agent), `claude_run.py`, `runs.py`, `usage.py`, `envs.py` |
| **Tracker sync** (anti-corruption layer to Jira) | StatusChange, changelog parsing, outbox moves, stage mapping from Jira statuses | `events.py`, `outbox.py`, `jira_session.py`, `integrations.py`, `pipeline.py` |
| **Workspace setup** (supporting) | Config, repo links, bot accounts, onboarding, preflight | `config.py`, `bots.py`, `onboarding.py`, `preflight.py` |

Context rules: Board is the center. Delivery reads Board facts and issues Board commands.
Execution is told what to run by Delivery. Tracker sync translates Jira into Board commands
and Board changes into outbox moves. Contexts talk through application use cases or domain
events, not by reading each other's tables.

## Target layout

```
src/wf/
  domain/                 pure; imports only wf.domain and the stdlib
    board/                ticket, stage, sprint, flag, approval, block, errors
    delivery/             routing, advancement (decide), refinement, idle, pause, merge_gate
    execution/            run, role, usage
    tracker/              status_change, changelog, stage_mapping, outbox_rules
    shared/               value objects, Clock-free time types
  application/            use cases; imports wf.domain only
    ports/                driven ports (protocols), see below
    board/ delivery/ execution/ tracker/    commands and queries, one class per use case
  adapters/               implement ports; import application + domain
    persistence/sqlite/   repositories, unit of work, schema
    tracker/jira/         REST changelog reader, Claude-session Jira executor
    forge/                github, gitlab (gh / glab CLI)
    model/                anthropic_api, claude_code, ollama
    workspace/            git worktrees, diff, head, sync; test command runner
    runner/               claude_code agent runner
    env/                  local app process
  entrypoints/            driving adapters; call use cases only
    cli/  web/ (pages, SSE)  gateway/ (MCP, scoped)  webhooks/  scheduler/ (auto loop)
  bootstrap.py            the composition root; the only module that imports every adapter
```

The package keeps the name `wf`. Old module paths stay as thin re-exports during migration so
tests and the running server keep working, then they are deleted in the last phase.

## Ports

Driven (core calls out): `TicketRepository`, `SprintRepository`, `EventLog` (history and
status changes), `OutboxRepository`, `RunRepository`, `UsageLedger`, `PauseState`,
`ApprovalStore`, `UnitOfWork`, `ModelPort` (exists), `ForgePort` (exists, finish),
`TrackerPort` (read changelog and observations, execute sends), `WorkspacePort` (worktree,
head, diff, sync), `TestRunnerPort`, `AgentRunnerPort`, `AppEnvironmentPort`, `Clock`,
`CredentialStore`.

Driving (outside calls in): the use cases themselves. CLI, web, webhooks, the MCP gateway,
and the scheduler are adapters that translate their input into a use case call. The
gateway's `Scope` is an authorization rule, so it moves into the application layer.

## The key pattern: facts in, decision out

`advance_pipeline`, `idle_reason`, the merge check, and `may_move` mix SQL with rules.
Split each into two steps:

1. A use case loads a snapshot of facts through ports (`TicketFacts`: stage, flags, latest QA
   outcome, approvals at head, runs at stage, unmet blockers, sprint active, paused).
2. A pure function `decide(facts) -> Decision` returns what to do and why.

This is also how open bug 1 (the harness advancing a flagged ticket) becomes a one-line rule
with a pure unit test, once the domain exists.

## Dependency rules, enforced

A pytest test parses imports with `ast` (no new dependency) and fails on any breach:

- `domain` imports only `wf.domain` and stdlib. No `sqlite3`, `subprocess`, `httpx`,
  `fastapi`, `time.time`, `os.environ`.
- `application` imports `wf.domain` and `wf.application`.
- `adapters` import `application` and `domain`, never `entrypoints`.
- `entrypoints` import `application` and `domain` types, never `adapters`.
- Only `bootstrap.py` imports `adapters` and `entrypoints` together.

Phase 0 records every current violation in an allowlist. The allowlist may only shrink, so
each phase can be merged while the rest is still unmigrated.

## Testing

- Domain: plain unit tests, no fixtures.
- Application: in-memory fakes of the ports.
- Adapters: one contract test suite per port, run against both the SQLite adapter and the
  in-memory fake, so the fakes stay faithful.
- Entrypoints: FastAPI `TestClient` with a test container.
- The current 352 tests are the safety net. Each phase keeps them green, then re-homes the
  ones for the migrated code. Before a risky rule moves (`advance_pipeline`, the merge gate),
  add characterization tests that pin today's outputs.

## Phases

Each phase ends with the full suite green and a restart of the server on a smoke check.
Sizes are relative.

0. **Guardrails (S).** Create the package skeleton, the import-rule test with the allowlist,
   and the characterization tests for `advance_pipeline`, `idle_reason`, `route`, and the
   merge check.
1. **Domain core (M).** Move the pure rules into `domain/`: stage and owner tables,
   `may_move`, routing, refinement turns, mapping, changelog parsing, `checks_state`,
   poll-due, and the merge and publish gates. Introduce the entities and value objects
   (Ticket, Stage, Role, Flag, Approval, Block, Run). Old modules call the new functions.
2. **Persistence ports (L).** Define the repository ports and `UnitOfWork`. Build the SQLite
   adapter by moving the SQL out of `board.py`, `outbox.py`, `events.py`, `runs.py`,
   `usage.py`, `pause.py`, and `agents.py`. Add the in-memory fakes and contract tests. The
   schema is untouched.
3. **Application layer (L).** One use case per command and query (sync payload, move stage,
   set and clear flag, record approval, advance pipeline, pickup, run agent, merge, publish,
   outbox plan and ack, pause and resume). Introduce the `decide(facts)` split. This is where
   `board.py` (876 lines) and `agents.py` (522 lines) dissolve.
4. **External ports (M).** Finish `ForgePort`. Add `TrackerPort`, `WorkspacePort`,
   `TestRunnerPort`, `AgentRunnerPort`, `AppEnvironmentPort`, `Clock`, and `CredentialStore`.
   Move `subprocess`, `gh`, `glab`, `claude`, and HTTP calls into adapters.
5. **Entrypoints and DI (M).** Build `bootstrap.py`. Rewrite the CLI (923 lines), the web
   app (pages and SSE), the webhooks, the gateway (`gateway_server.py`, 454 lines), and the
   scheduler to call use cases from the container. Remove module-level construction and
   global config reads.
6. **Domain events (optional, M).** Entities emit `TicketMoved`, `FlagSet`,
   `ApprovalRecorded`. Subscribers write the history, queue outbox moves, and feed the SSE
   stream, replacing the board diffing.
7. **Close out (S).** Delete the re-export shims, set the allowlist to empty, and write
   `docs/architecture.md` and short ADRs for the decisions below.

## How the open items fit

- Bug 1 (flagged tickets still advance, QA approving visual criteria): a pure rule in
  phase 1 or 3. The prompt fix is independent and can go first.
- Headless browser for QA (item 4): a `BrowserPort` and one adapter, after phase 4.
- Add-ticket via the Jira agent (item 5): a use case plus `TrackerPort`, after phase 4.
- Ticket page live updates (item 7): simpler after phase 6.
- Porting the Fokist-specific pieces (item 6): becomes a configuration and adapter package
  in the private repo, after phase 5. Do not port into the old copy before then.

## Risks

- The tests pass a raw `sqlite3.Connection` almost everywhere. Re-homing them is most of the
  cost of phases 2 and 3.
- `UnitOfWork` must preserve today's transaction boundaries, which are implicit in the shared
  connection. Phase 2 starts by documenting them.
- The live board data (`wf.sqlite`) means no schema changes, and each phase restarts the
  server on a copy of that file first.
- The repo is public: no project names, hosts, or paths in code, docs, or fixtures.

## Decisions (settled)

1. **Base branch.** The runner branch goes to `main` through a PR first. Each unit of work
   after that: pull latest `main`, branch off, PR when the unit is cohesive and complete.
2. **LangGraph shell (`graph.py`).** Keep it, as an adapter behind a `WorkflowEnginePort`,
   following LangGraph practice: the graph is compiled in the composition root with the
   checkpointer and dependencies injected, and nodes take their dependencies from there.
   It is used only by `wf run` and the Runs pages, so it moves late (phase 4 or 5).
3. **Domain events (phase 6).** Deferred. Decide after phase 5.
4. **Layout.** Subpackages as above.
5. **DI.** Hand-written composition root.

## Working rule

Do only what is certain now. Anything that can wait waits: phase 6, the optional ports for
new features (browser, add-ticket), and the Fokist-specific port are not started until the
phases they depend on are merged.
