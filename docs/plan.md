# Plan: architecture and decisions

Status: the discovery, status mapping, board, and Claude Code run work is in progress.
Decisions here are current. Items marked **open** still need a decision.

## 1. What this is

`wf` is a local, multi-agent workflow. Agents take tickets from a team's Jira sprint
and work them in internal git repositories. A person makes the decisions that matter:
what goes into a sprint, when code leaves the internal repos, and anything the agents
can't settle.

The tool is open source. Each team configures it for its own Jira projects, workflow
statuses, and repositories. Nothing in the code or the default prompts is specific to
one product.

## 2. Sources of truth

| Data | Owner | Direction |
|---|---|---|
| Tickets in the user's open sprints, their fields, changelog | Jira | Read through the user's Atlassian MCP |
| Projects, boards, statuses, sprint and custom field IDs | Jira | Discovered at startup and stored as a snapshot |
| Stage a ticket is in while agents work | The local board (SQLite) | Written by the harness only |
| Branches, internal PRs, merges | Internal git repos | Written by agents, through the gateway |
| Runs, events, token use | SQLite | Written by the harness |

Jira's status is shown on the board as a mirror. Internal agents never reach outside the
internal board. Only two bots reach outside it:

- **The Jira agent** is the only component that writes to Jira. It pulls tickets in, and
  it pushes board changes out (for example, moving the real ticket from In Progress to In
  Review once the internal stage changes).
  - It acts through the user's Atlassian MCP connection, so writes show as the user.
  - It writes without asking for approval.
  - Its scope is any interaction with tickets assigned to the user: status transitions,
    comments, and field updates. It does not touch tickets assigned to anyone else.
  - Before each write it checks the ticket's current Jira status, sends only a transition
    Jira allows from there, and records Jira's response so retries can't apply a change
    twice.
- **The GitHub agent** is the only component that writes to the org's repositories. It
  opens a real pull request only after the work has passed the whole internal pipeline:
  research, development, review, QA, performance, and sprint regression. Until then the
  work stays on the internal board and in the internal repos.

Everything else works only inside the internal board and the internal repos.

## 3. Discovery

Discovery runs from the user's Claude session, because the Atlassian MCP lives there.
The tool can't call it on its own. A user without the MCP connected can't use the tool.
The boot check looks for the connection and stops with setup steps when it's missing.

Discovery records:

- the site and the signed-in account
- each project's key, name, issue types, and statuses, with Jira's status categories
- each board's active sprints
- the field IDs for sprints and any custom fields the workflow reads

The snapshot is stored in SQLite with a timestamp. Discovery re-runs when the snapshot
is stale or on request.

**Limit:** the MCP tools here don't expose a project's full status list. Statuses are
observed from tickets, so a status that no ticket uses yet is missing. The snapshot
records where each status came from, so a missing status shows up as a gap rather than
a silent failure.

## 4. Status mapping

There is no default status list. The tool has a fixed set of internal stages, each with
a job the agents do, such as refinement, ready for development, in development, review,
QA, merged, regression, and deployed. Each stage is mapped automatically to one of the
user's statuses, by name and Jira category.

- A stage with exactly one matching status is mapped.
- A stage with several matches, or none, is **unmapped**. The board shows it as blocked.
  Routing never guesses.
- A status that matches more than one stage is left out of the mapping and reported.

The mapping is saved with the snapshot. `wf` shows it, and it is part of every route
decision.

## 5. The sprint board

The board holds the signed-in user's tickets in the open sprint. It is the only place
agents move work.

- **Scope:** tickets assigned to the user in an open sprint. No label filter.
- **Removal:** a ticket that leaves the sprint or changes assignee is marked `removed`.
  Work in progress is kept and flagged for the user.
- **Events:** every sync, stage change, flag, comment, and run is appended to an event
  log. Nothing edits or deletes it.
- **Flags:** `needs-human` and `blocked`. Agents can set them. Only the user clears them.
- **Sync:** on demand, from the session. A scheduled sync needs a Jira API token on the
  harness, which is a separate decision.

Details are in `docs/internal-board.md` on the `design/internal-board` branch.

## 6. Agents

Each role has a prompt in `prompts/`:

| Role | Job |
|---|---|
| Product | Helps the user shape features and the roadmap. Proposes tickets. |
| Staff | Scopes tickets, writes plans and decision records, assigns work, reviews planning. |
| Dev | Implements one ticket test-first in its own branch, and opens an internal PR. |
| PR review | Reviews internal PRs for technical quality. Four outcomes. |
| QA | Tests each ticket and the sprint regression. Second approval. |
| Performance | Tests performance ideas on branches, before code is pushed out. |

Internal agents never write to Jira and never push to the org's hosting. Only the Jira
agent and the GitHub agent do, as described in section 2.

Prompts are generic. The project's own rules come from its configuration and from the
repo's AI instructions file.

## 7. Repos and the cycle

- Each linked repo is a local git repository. Its `main` branch is the internal `main`.
- Dev works on `agent/<KEY>-<slug>`, cut from the internal `main`.
- An internal PR is reviewed by the PR review agent. QA and performance run against the
  branch before anything leaves the machine.
- A branch merges into the internal `main` after both reviews pass. The merge rule is
  checked by the harness: two approvals from two different roles, on the current head,
  with no open threads and passing tests.
- **Decided:** the GitHub agent opens a real pull request on the org's hosting only after
  the work has passed the whole internal pipeline, including performance and sprint
  regression (section 2).

## 8. Models and access

- **`ollama`:** a local model. No cloud terms apply.
- **`anthropic-api`:** the Claude API, billed per token, with an API key from the
  environment.
- **`claude-code`:** the Claude Code CLI, run on the host under the operator's own
  login. This is the first flow. It is off by default and needs `claude_code_access`
  set on the machine.

Runs strip `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the child process, so a
subscription run never bills an API key by accident. `wf doctor` checks this.

The first flow targets Apple Silicon Macs. See `docs/mac-setup.md`.

## 9. Runs and safety

- A Claude Code run starts in the ticket's worktree with a time limit.
- Its MCP config connects it to the gateway only. Other MCP servers are not loaded.
- Its built-in tools are the file tools for the worktree. Shell access is left out.
  Tests run through a gateway tool.
- Writes go through the gateway, which checks role and scope and keeps an audit log.
- The run's output is saved in its run directory.

The container runner and the Linux runtime are not built yet.

## 10. Build order

1. Done: the board schema in SQLite, and the sync payload from the session (`docs/sync-format.md`).
2. Done: automatic status mapping (`src/wf/domain/board/stages.py`). Unmapped stages are blocked, and
   routing uses the mapping in place of the old fixed status table.
3. Partly done: `wf status` reports when the board last synced, whether it's stale, and
   what is blocked. The MCP connection itself can only be checked inside the Claude
   session, so the session checks it before it runs a sync.
4. Done: remove project-specific code and docs.
5. Done: the sprint board. `wf sync`, `wf board`, `wf backlog`, and `wf sprints` work
   against the payload. `wf move` lets the harness or an agent role move a ticket to a
   mapped stage, and every move is logged. `wf flag` lets agents flag a ticket for a
   human, and only the user can clear a flag.
6. Not yet: gateway tools for agents, then the run loop.
7. Done: the Jira outbox (`src/wf/application/tracker/outbox.py`, `docs/outbox.md`). Stage moves are queued in
   SQLite. The Jira agent plans them against the observed Jira state, sends at most once,
   and records each result. Comments and field updates are not built yet.
8. Done: the board UI (`src/wf/entrypoints/web/board_views.py`). Pages for the active sprint, the
   backlog, one ticket, and the Jira outbox. The user can clear flags and retry outbox
   items there. Agents don't use these pages. The gateway will call `move` and
   `flag` on behalf of an agent, and check its role first.

## 11. Open items

- Whether agents may create tickets on the board, and how those stay off the sync.
- How to handle a ticket that changes in Jira while an agent works on it.
- Whether a ticket can belong to two open sprints.
- Scheduled sync, which needs a Jira API token on the harness.
- **Allowed moves per role.** The table of (role, from-stage, to-stage) is in
  `src/wf/domain/board/moves.py` (`ALLOWED_MOVES`) and is enforced when a move is made. It was derived
  from the agent prompts and is not a complete list of guardrails. It is expected to grow
  as the project does. The actor is still a label the caller gives, so the gateway must
  set it before any agent connects.
