# Plan: a plugin system

Status: proposal, nothing built. Branch: `plugins`.

## Goal

New features ship as versioned plugins on top of a core that changes slowly. Someone running `wf` for a single
workflow upgrades the core without anything changing for them, and adds a feature (for example, orchestrating
several workflows) by installing and enabling its plugin. The same plugin version behaves the same everywhere.

Not goals: hot reloading, a marketplace, running untrusted code in a sandbox, or plugins that replace the core's
domain rules.

## Terms

| Term | Meaning |
|---|---|
| **Core** | This package: the domain, the use cases, the adapters, the entrypoints. |
| **Plugin** | A separate Python package that adds behavior through the plugin API. |
| **Plugin API** | `wf.plugin_api`, the only part of `wf` a plugin may import. Versioned on its own (API 1.x). |
| **Extension point** | One place the core lets plugins add something: an adapter, an event handler, a command, a screen. |
| **Host** | The object the core hands a plugin when it registers. The plugin tells the host what it adds. |
| **Manifest** | What a plugin says about itself: name, version, the API versions it supports, what it needs and conflicts with. |

## What a plugin looks like

```python
# wf_multiflow/__init__.py, in a package named wf-plugin-multiflow
from wf.plugin_api import Plugin, Manifest

class Multiflow(Plugin):
    manifest = Manifest(
        name="multiflow", version="0.1.0", api=">=1,<2",
        requires=[], conflicts=[], settings=MultiflowSettings)

    def register(self, host):
        host.migrations("multiflow", MIGRATIONS)          # its own tables, its own numbered steps
        host.subscribe(TicketMoved, on_ticket_moved)      # react to core events
        host.command("workflows", add_parser, run)        # `wf workflows ...`
        host.screen("/workflows", WorkflowsViewModel, templates=HERE / "templates", tab="Workflows")
        host.cycle_step(orchestrate)                      # runs on each wake of the auto loop
```

It declares itself in its `pyproject.toml`:

```toml
[project.entry-points."wf.plugins"]
multiflow = "wf_multiflow:Multiflow"
```

## Extension points in API v1

| Extension point | Host call | Used for |
|---|---|---|
| Adapters for ports | `host.provide(Port, name, factory)` | Another tracker, forge, model, agent runner. The config picks which one by name. |
| Event handlers | `host.subscribe(EventType, handler)` | Reacting to `TicketMoved`, `ApprovalRecorded`, ... inside the same transaction. |
| Use-case access | `host.context` | A plugin's own use cases get a small, stable view of the core: ticket commands, board queries, settings, the clock, the activity log. |
| CLI commands | `host.command(name, add_parser, run)` | `wf <name> ...` |
| Web screens | `host.screen(path, view_model, templates, tab)` | A page with its own view model and templates, and a navigation tab. |
| Loop steps | `host.cycle_step(fn)` | Work done on each wake of the auto loop, after the core's own steps. |
| Migrations | `host.migrations(namespace, steps)` | The plugin's own tables, created and changed by numbered steps. |

Kept for a later API version, when a real plugin needs them: gateway tools for agents, new stages or roles, and named
policy points where a plugin may extend a rule (for example, extra allowed moves).

A plugin may add; it may not change what the core does. Core rules stay in the core.

## What the core needs first

1. **Numbered migrations.** Today the schema is built by `create table if not exists` in seven places and by
   `alter table` checks in `schema.connect()`, every time a database opens. Replace that with one migration runner:
   a `schema_migrations(namespace, version, applied_at)` table, the core's steps as namespace `core` (today's schema
   is step 1, the existing `alter table` fixes are steps 2 and on), and the same runner for each plugin's namespace.
   Opening a database no longer changes it. `wf upgrade` backs up `wf.sqlite`, runs what is missing, and reports it;
   `wf upgrade --dry-run` shows the plan. A database newer than the installed code is refused.
2. **Versioned config.** `workflow.json` gets `schema_version` and a `plugins` section: which plugins are enabled,
   and their settings. A `wf.lock` file records the exact core and plugin versions, so an install can be reproduced.
3. **Workflow scoping.** Orchestrating several workflows needs the core to know which workflow a ticket, sprint, run,
   or outbox item belongs to. Adding that after API 1.0 would break every plugin, so it goes in now: a `workflow`
   table and a `workflow_id` on the scoped tables, defaulting to `default`. A single-workflow user never sees it.
4. **A plugin host in the composition root.** `Container` discovers plugins, checks their manifests, and collects what
   each registers. The event bus, the adapter choice, the CLI parser, the web app, and the auto loop read from it.
5. **Extensible entrypoints.** The CLI builds its subcommands from the core plus the plugins. The web app mounts
   plugin routes, adds their tabs, and finds their templates through a Jinja loader chain.
6. **The plugin API module.** `wf.plugin_api` re-exports exactly what plugins may use, and has its own version.
   A test snapshots its public names and signatures, so a change to it is always deliberate and versioned.
7. **A test kit.** `wf.plugin_testing` gives plugin authors a fake host, the port contract tests to run against their
   adapters, and a fixture that runs their migrations against a temporary database.

## Lifecycle

1. **Discover:** read the `wf.plugins` entry points.
2. **Check:** the manifest's API range matches this core; what it requires is present and enabled; nothing it
   conflicts with is enabled. A plugin that fails a check is not loaded, and the reason is shown.
3. **Configure:** only plugins enabled in `workflow.json` go further. Their settings are validated.
4. **Migrate:** only on `wf upgrade`. If an enabled plugin has migrations waiting, `wf` says so and does not start.
5. **Register:** each plugin tells the host what it adds.
6. **Run:** the core uses the registered additions. A plugin that raises while handling something is reported in the
   activity log, and the core keeps running.

## Versions, compatibility, and updates

- The core follows semantic versioning. The plugin API has its own version and stays compatible within a major
  version. Removing or changing anything in it takes a deprecation warning for one minor release first.
- `wf plugins list` shows what is installed, enabled, and compatible. `wf plugins check` says, before a core upgrade,
  which installed plugins support the new version.
- `wf update --check` (opt-in) shows newer core and plugin releases and what changed. Nothing installs by itself.
- First-party plugins live in this repo under `plugins/`, each its own package with its own version.

## Trust

A plugin is ordinary Python code with the same access as `wf` itself. The core never downloads or installs one; a
person installs it on purpose. The README says so plainly.

## Proving it

- **`plugins/example`**: a small plugin that uses every v1 extension point (a migration, an event handler, a command,
  a screen, a loop step). It doubles as the template for plugin authors and as an end-to-end test.
- **`plugins/gitlab`**: move the GitLab forge adapter out of the core into a first-party plugin, to prove the adapter
  extension point on something real. GitHub stays in the core.
- **Multi-workflow orchestration**: the first new feature built as a plugin, with its own plan once the API exists.

## Phases

Each phase ends with the full suite green and a PR.

1. **Migrations.** The runner, the core's steps, `wf upgrade` with backup and dry run, the newer-database guard.
   Done when a database from the current `main` and a fresh one both migrate to the same schema, running twice
   changes nothing, and a saved database from each release is migrated in the tests.
2. **Config.** `schema_version`, the `plugins` section, `wf.lock`, and migration of an old config file.
3. **Plugin host and API v1, part one.** `wf.plugin_api`, manifests, discovery, compatibility checks, and the adapter,
   event, use-case access, command, and migration extension points. `plugins/example` with those parts. The test kit.
4. **Plugin API v1, part two.** Web screens, tabs, and templates, and loop steps. `plugins/example` gains a screen.
5. **Workflow scoping.** The `workflow` table and `workflow_id` with an implicit `default`, through the domain, the
   use cases, and the queries. No visible change for a single workflow.
6. **First-party plugin and tooling.** `plugins/gitlab`, `wf plugins list` and `check`, `wf update --check`, the plugin
   authoring guide, and the README section on plugins. The plugin API is declared 1.0.
7. **Multi-workflow orchestration**, as a plugin, planned separately.

## Decisions needed

1. **How plugins are found.** Recommended: pip packages declared through entry points, the standard Python way.
   Optionally also a local `plugins/` folder for development.
2. **Workflow scoping in the core now.** Recommended: yes, before the API is frozen at 1.0 (phase 5).
3. **New stages or roles from plugins.** Recommended: not in v1. The stage set is part of the core's language.
4. **Where first-party plugins live.** Recommended: this repo, under `plugins/`, each its own package and version.
5. **The update check.** Recommended: opt-in.
