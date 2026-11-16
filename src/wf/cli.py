"""`wf` command: link and check internal repos and Jira boards, and run the workflow shell."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wf import config as cfgmod
from wf import graph
from wf.integrations import JiraClient, check_repo


def _cfg_path(args) -> Path:
    return Path(args.config) if args.config else cfgmod.default_path()


def cmd_link_repo(args) -> int:
    path = _cfg_path(args)
    c = cfgmod.load(path)
    if c.add_repo(args.name, args.repo_path):
        cfgmod.save(c, path)
        print(f"linked repo {args.name} at {args.repo_path}")
    else:
        print(f"repo {args.name} already linked")
    return 0


def cmd_link_board(args) -> int:
    path = _cfg_path(args)
    c = cfgmod.load(path)
    if c.add_board(args.provider, args.ref, args.label or ""):
        cfgmod.save(c, path)
        print(f"linked {args.provider} board {args.ref}")
    else:
        print(f"{args.provider} board {args.ref} already linked")
    return 0


def cmd_unlink(args) -> int:
    path = _cfg_path(args)
    c = cfgmod.load(path)
    if args.kind == "repo":
        removed = c.remove_repo(args.ref)
    else:
        removed = c.remove_board(args.kind, args.ref)
    if removed:
        cfgmod.save(c, path)
        print(f"unlinked {args.kind} {args.ref}")
        return 0
    print(f"{args.kind} {args.ref} is not linked", file=sys.stderr)
    return 1


def cmd_list(args) -> int:
    c = cfgmod.load(_cfg_path(args))
    if not c.repos and not c.boards:
        print("nothing linked yet")
        return 0
    for r in c.repos:
        print(f"repo   {r.name:<12} {r.path}")
    for b in c.boards:
        label = f"  ({b.label})" if b.label else ""
        print(f"board  {b.provider:<6} {b.ref}{label}")
    return 0


def cmd_check(args) -> int:
    c = cfgmod.load(_cfg_path(args))
    jira = JiraClient()
    failures = 0
    for r in c.repos:
        res = check_repo(r)
        failures += not res.ok
        print(f"{'ok  ' if res.ok else 'FAIL'} repo  {r.name}: {res.detail}")
    for b in c.boards:
        res = jira.check_board(b)
        failures += not res.ok
        print(f"{'ok  ' if res.ok else 'FAIL'} board {b.provider} {b.ref}: {res.detail}")
    if not c.repos and not c.boards:
        print("nothing linked to check")
    return 1 if failures else 0


def cmd_run(args) -> int:
    out = graph.start(Path(args.db), args.thread, args.request)
    _print_run(out)
    return 0


def cmd_usage(args) -> int:
    import os
    from wf import usage

    ledger = usage.UsageLedger(Path(args.db or os.environ.get("WF_DB", "wf.sqlite")))
    print(usage.format_audit(ledger.audit(), ledger.largest_calls()))
    return 0


def _db_path(args) -> Path:
    import os

    return Path(args.db or os.environ.get("WF_DB", "wf.sqlite"))


def cmd_route(args) -> int:
    from wf import orchestrator

    labels = {"hotfix"} if args.hotfix else set()
    mapping = orchestrator.board_status_to_stage(_db_path(args))
    d = orchestrator.route(args.status, labels, mapping)
    print(f"action: {d.action}\nowner:  {d.owner or '-'}\nwhy:    {d.reason}")
    return 0


def cmd_sync(args) -> int:
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        result = board.sync_file(conn, Path(args.from_file))
    except board.BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    summary = ", ".join(f"{k}={v}" for k, v in sorted(result.counts.items()))
    print(f"synced: {summary or 'nothing to change'}")
    for problem in result.problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 0


def cmd_board(args) -> int:
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        rows = board.active_board(conn)
        if not rows:
            print("no tickets in the active sprint (run `wf sync` first)")
        current = None
        for r in rows:
            stage = r["stage"] or "(unmapped)"
            if stage != current:
                print(f"\n{stage}")
                current = stage
            print(f"  {r['key']:<12} {r['jira_status']:<20} {r['summary']}")
        for b in board.unmapped_stages(conn):
            print(f"blocked: {b['project_key']} stage {b['stage']!r}: {b['reason']}", file=sys.stderr)
    finally:
        conn.close()
    return 0


def cmd_backlog(args) -> int:
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        rows = board.backlog(conn)
        if not rows:
            print("the backlog is empty (run `wf sync` with a backlog payload first)")
        current = None
        for r in rows:
            stage = r["stage"] or "(unmapped)"
            if stage != current:
                print(f"\n{stage}")
                current = stage
            print(f"  {r['key']:<12} {r['jira_status']:<20} {r['summary']}")
    finally:
        conn.close()
    return 0


SOURCE_OPTIONS = [
    ("1", "Local GitLab (on this network, e.g. your home server)"),
    ("2", "Remote GitLab (gitlab.com or another hosted GitLab)"),
    ("3", "Local git only (no hosted remote)"),
    ("4", "Remote GitHub (github.com)"),
]


def _source_control(c, interactive: bool) -> tuple[str, str, str]:
    """Ask where the repo's code is hosted. Returns (forge, host, org/repo)."""
    if not interactive:
        if c.remote and "github.com" in (c.remote_url or ""):
            return "github", "", c.remote
        if c.remote_url:
            return "gitlab", "", c.remote
        return "local", "", ""
    print(f"\nWhere is the source control for {c.path}?")
    for key, label in SOURCE_OPTIONS:
        print(f"  {key}. {label}")
    choice = _ask("Choose 1-4", "4" if c.remote_url and "github.com" in c.remote_url else "3")
    if choice == "3":
        return "local", "", ""
    if choice == "4":
        return "github", "", c.remote or _ask("Org/repo it pushes to (owner/name)", "")
    host = _ask("GitLab host (e.g. gitlab.home.lan or gitlab.com)", "gitlab.com")
    project = c.remote or _ask("GitLab project path (group/project)", "")
    return "gitlab", host, project


def _ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def cmd_init(args) -> int:
    from wf import onboarding

    path = _cfg_path(args)
    interactive = sys.stdin.isatty()

    # 1. The Atlassian MCP lives in the Claude session, so the user connects it there.
    print("Step 1: connect the Atlassian MCP.")
    print("  In Claude Code, connect the Atlassian (Jira) MCP and sign in with your Atlassian account.")
    print("  Then ask Claude to confirm the connection.")
    if not args.mcp_connected:
        if not interactive:
            print("Re-run with --mcp-connected once the MCP is connected.", file=sys.stderr)
            return 2
        if _ask("Is the Atlassian MCP connected? (y/n)", "n").lower() not in ("y", "yes"):
            print("Connect the MCP, then run `wf init` again.")
            return 0

    # 2. Find the repos the user works on.
    roots = args.root
    if not roots:
        if not interactive:
            print("Pass --root FOLDER to say where your code lives.", file=sys.stderr)
            return 2
        roots = [_ask("Step 2: folder where you keep your code", "~/code")]
    candidates = onboarding.suggest([Path(r) for r in roots])
    if not candidates:
        print("No git repositories found under " + ", ".join(roots) + ".")
        return 0

    print("\nRepos found, most worked on first:")
    for i, c in enumerate(candidates, 1):
        remote = c.remote or "(no origin)"
        print(f"  {i:>2}. {c.name:<20} {remote:<32} your commits: {c.my_commits}  {c.path}")

    # 3. The user picks the repos to link.
    picks = args.pick
    if picks is None:
        if not interactive:
            print("Re-run with --pick 1,2,... to choose repos to link.")
            return 0
        picks = _ask("Step 3: which repos do you work on? (numbers, comma-separated)", "")
    chosen: list[int] = []
    for token in picks.replace(" ", "").split(","):
        if not token:
            continue
        if not token.isdigit() or not 1 <= int(token) <= len(candidates):
            print(f"error: {token!r} is not one of the numbers above", file=sys.stderr)
            return 2
        chosen.append(int(token))
    if not chosen:
        print("Nothing picked. Run `wf init` again when you're ready.")
        return 0

    # 4. Link them, with where each one's code is hosted.
    cfg = cfgmod.load(path)
    for n in chosen:
        c = candidates[n - 1]
        name = c.name
        if interactive:
            name = _ask(f"Name for {c.path}", c.name)
        forge_name, host, remote = _source_control(c, interactive)
        if cfg.add_repo(name, c.path, remote, forge=forge_name, host=host):
            where = {"github": "GitHub", "gitlab": f"GitLab at {host}", "local": "local git only"}[forge_name]
            print(f"linked {name} at {c.path} -> {where}{' ' + remote if remote else ''}")
        else:
            print(f"{name} is already linked")
    try:
        cfgmod.save(cfg, path)
    except OSError as e:
        print(f"error: could not save {path}: {e}", file=sys.stderr)
        return 2

    print("\nNext: in your Claude session, run the Jira sync (see docs/sync-format.md),")
    print("then `wf sync payload.json` and `wf doctor`.")
    if interactive:
        from wf import bots as _bots

        c_ = cfgmod.load(path)
        if not c_.bots.enabled:
            result = _bots.setup_interactive(c_.bots.domain or "fokist.com")
            if result:
                c_.bots = cfgmod.BotsConfig(**result)
                cfgmod.save(c_, path)
    return 0


def _load_json(path: str):
    import json

    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"error: could not read {path}: {e}")


def cmd_outbox(args) -> int:
    import json
    from wf import board, outbox

    conn = board.connect(_db_path(args))
    try:
        if args.outbox_cmd == "list":
            print(json.dumps(outbox.listing(conn, args.status), indent=2))
        elif args.outbox_cmd == "pending":
            print(json.dumps(outbox.pending(conn), indent=2))
        elif args.outbox_cmd == "plan":
            data = _load_json(args.from_file)
            print(json.dumps(outbox.plan(conn, data.get("observations", data)), indent=2))
        elif args.outbox_cmd == "ack":
            print(json.dumps(outbox.ack(conn, _load_json(args.from_file)), indent=2))
        elif args.outbox_cmd == "retry":
            outbox.retry(conn, args.id)
            print(f"item {args.id} is pending again")
    except board.BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


def cmd_status(args) -> int:
    import time
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        s = board.status(conn, time.time())
    finally:
        conn.close()
    run = s["last_sync"]
    if run is None:
        print("never synced: run the sync from your Claude session, then `wf sync`")
    else:
        age = s["age_seconds"]
        age_text = "unknown" if age is None else f"{age // 3600}h {age % 3600 // 60}m ago"
        print(f"last sync: {run['kind']} {run['status']}, {age_text}")
        if run["status"] == "failed":
            print(f"  error: {run['error']}")
        elif age is not None and age > 24 * 3600:
            print("  stale: run a sync from your Claude session")
    print(f"active sprint: {s['active_tickets']} tickets; backlog: {s['backlog_tickets']} tickets")
    for f in s["flagged"]:
        print(f"flagged: {f['key']} {f['flags']}")
    for u in s["unmapped_stages"]:
        print(f"blocked: {u['project_key']} stage {u['stage']!r}: {u['reason']}")
    return 0


def cmd_pickup(args) -> int:
    import json

    from wf import board, pickup

    conn = board.connect(_db_path(args))
    try:
        plan = pickup.plan_pickup(conn, apply=args.apply)
    finally:
        conn.close()
    print(json.dumps(plan.to_dict(), indent=2))
    if not args.apply:
        print("dry run: nothing was moved. Re-run with --apply to move the picked tickets.", file=sys.stderr)
    return 0


def cmd_move(args) -> int:
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        board.move_stage(conn, args.key, args.stage, args.actor, args.reason)
    except board.BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"{args.key} -> {args.stage}")
    return 0


def cmd_flag(args) -> int:
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        if args.clear:
            board.clear_flag(conn, args.key, args.flag, args.actor)
            print(f"cleared {args.flag} on {args.key}")
        else:
            board.set_flag(conn, args.key, args.flag, args.actor, args.reason or "")
            print(f"flagged {args.key} {args.flag}")
    except board.BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


def cmd_sprints(args) -> int:
    from wf import board

    conn = board.connect(_db_path(args))
    try:
        for s in board.sprints(conn, args.state):
            print(f"{s['state']:<7} {s['id']:<8} {s['name']}")
    finally:
        conn.close()
    return 0


def cmd_kill(args) -> int:
    import os
    from wf import orchestrator

    store = orchestrator.LifecycleStore(Path(args.db or os.environ.get("WF_DB", "wf.sqlite")))
    if args.state == "status":
        print("kill switch is", "ON" if store.killed() else "off")
    else:
        store.set_kill(args.state == "on")
        print("kill switch is now", args.state)
    return 0


def cmd_poll(args) -> int:
    import os
    from wf.integrations import JiraClient
    from wf.pipeline import Pipeline

    jql = os.environ.get("WF_POLL_JQL", "updated >= -2h ORDER BY updated DESC")
    jira = JiraClient()
    pipe = Pipeline(
        Path(args.db or os.environ.get("WF_DB", "wf.sqlite")),
        fetch_changelog=jira.get_issue_changelog,
        search_keys=lambda: jira.search_keys(jql),
    )
    decisions = pipe.poll()
    if not decisions:
        print("poll skipped (webhooks recent) or nothing new")
    for d in decisions:
        print(f"{d.action:<9} {d.owner or '-':<14} {d.reason}")
    return 0


def cmd_resume(args) -> int:
    out = graph.resume(Path(args.db), args.thread, approved=args.approve)
    _print_run(out)
    return 0


def _print_run(out: dict) -> None:
    print("steps:", " -> ".join(out["steps"]))
    print("status:", out["status"])


def cmd_model_status(args) -> int:
    import os

    c = cfgmod.load(_cfg_path(args))
    m = c.model
    if m.provider == "anthropic-api":
        key_state = (f"{m.api_key_env} is set" if os.environ.get(m.api_key_env)
                     else f"{m.api_key_env} is NOT set")
    elif m.provider == "claude-code":
        key_state = "none; uses this machine's Claude Code login"
    else:
        key_state = "not needed"
    print(f"provider:    {m.provider}")
    print(f"model:       {m.model}")
    if m.provider == "ollama":
        print(f"base url:    {m.base_url}")
    print(f"credentials: {key_state}")
    print(f"claude code: {'enabled on this machine' if m.claude_code_access else 'not enabled'}")
    return 0


def cmd_model_set(args) -> int:
    from dataclasses import replace

    path = _cfg_path(args)
    c = cfgmod.load(path)
    updates = {k: v for k, v in {"provider": args.provider, "model": args.model,
                                 "base_url": args.base_url, "api_key_env": args.api_key_env}.items() if v}
    if args.claude_code_access is not None:
        updates["claude_code_access"] = args.claude_code_access
    c.model = replace(c.model, **updates)
    cfgmod.save(c, path)
    print(f"model set: provider={c.model.provider} model={c.model.model}")
    return 0


def _repo_path(c, name: str | None) -> Path:
    repos = {r.name: r for r in c.repos}
    if not repos:
        raise SystemExit("error: no internal repo is linked; run `wf link repo NAME PATH` first")
    if name is None:
        if len(repos) > 1:
            raise SystemExit(f"error: several repos are linked; pick one with --repo ({', '.join(sorted(repos))})")
        name = next(iter(repos))
    if name not in repos:
        raise SystemExit(f"error: no linked repo named {name!r}")
    return Path(repos[name].path)


def _runs_dir(c, given: str | None) -> Path:
    import os

    return Path(given or os.path.expanduser(c.agents.runs_dir)).resolve()


def cmd_agents_tick(args) -> int:
    import json
    import shutil

    from wf import agents, board, claude_run

    c = cfgmod.load(_cfg_path(args))
    conn = board.connect(_db_path(args))
    try:
        actions = agents.tick(
            conn, repo=_repo_path(c, args.repo), runs_dir=_runs_dir(c, args.runs), access=c.model.claude_code_access,
            model=args.model or c.agents.model, test_cmd=args.test_cmd or c.agents.test_cmd, app_cmd=c.agents.app_cmd, app_url=c.agents.app_url, bot_domain=c.bots.domain if c.bots.enabled else "", bot_credentials=c.bots.credentials_file, dry_run=not args.apply,
            claude_bin=shutil.which(claude_run.CLAUDE_BIN),
        )
    except (claude_run.ProviderNotAllowed, ValueError, board.BoardError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(actions, indent=2))
    if not args.apply:
        print("dry run: nothing was run. Re-run with --apply to run these agents.", file=sys.stderr)
    return 0


def cmd_agents_merge(args) -> int:
    from wf import board, merge

    c = cfgmod.load(_cfg_path(args))
    conn = board.connect(_db_path(args))
    try:
        head = merge.merge(conn, args.key, repo=_repo_path(c, args.repo), runs_dir=_runs_dir(c, args.runs),
                           test_cmd=args.test_cmd or c.agents.test_cmd)
    except (merge.MergeBlocked, board.BoardError) as e:
        print(f"blocked: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"{args.key} merged at {head[:8]}")
    return 0


def cmd_agents_publish(args) -> int:
    from wf import board, github_agent

    c = cfgmod.load(_cfg_path(args))
    conn = board.connect(_db_path(args))
    try:
        url = github_agent.open_pull_request(conn, args.key, repo=_repo_path(c, args.repo),
                                             confirm=args.confirm)
    except (github_agent.PublishBlocked, board.BoardError) as e:
        print(f"blocked: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(url)
    return 0


def cmd_bots_setup(args) -> int:
    from wf import bots

    path = _cfg_path(args)
    c = cfgmod.load(path)
    domain = args.domain or c.bots.domain or "fokist.com"
    if args.yes:
        written = bots.write_credentials(bots.credentials_path(),
                                         {r: {"email": bots.account_email(r, domain)} for r in bots.ROLES})
        print(f"credentials written to {bots.credentials_path()} (new: {', '.join(written) or 'none'})")
        result = {"enabled": True, "domain": domain, "roles": list(bots.ROLES),
                  "credentials_file": str(bots.credentials_path())}
    else:
        result = bots.setup_interactive(domain)
    if result:
        c.bots = cfgmod.BotsConfig(**result)
        cfgmod.save(c, path)
    return 0


def cmd_bots_permissions(args) -> int:
    from wf import bots

    print("Bots may:")
    for line in bots.ALLOWED:
        print(f"  + {line}")
    print("Bots may not:")
    for line in bots.NOT_ALLOWED:
        print(f"  - {line}")
    return 0


def cmd_env(args) -> int:
    from wf import envs

    c = cfgmod.load(_cfg_path(args))
    state_dir = _runs_dir(c, None) / "envs"
    if args.env_cmd == "status":
        st = envs.status(state_dir, args.name or "app")
        print("not running" if not st or not st["running"] else f"running at {st['url']} (pid {st['pid']})")
        return 0
    if args.env_cmd == "down":
        print("stopped" if envs.down(args.name or "app", state_dir=state_dir) else "nothing to stop")
        return 0
    repo = _repo_path(c, args.repo)
    try:
        st = envs.up(args.name or "app", repo=repo, app_cmd=c.agents.app_cmd, state_dir=state_dir, app_url=c.agents.app_url)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"running at {st['url']} (pid {st['pid']})")
    return 0


def cmd_run_claude(args) -> int:
    import shlex
    import shutil

    from wf import claude_run
    from wf.model import ProviderNotAllowed

    c = cfgmod.load(_cfg_path(args))
    try:
        result = claude_run.run(
            worktree=Path(args.worktree),
            prompt=Path(args.prompt_file).read_text(),
            system_prompt=Path(args.system_prompt_file).read_text(),
            model=args.model or c.model.model,
            run_dir=Path(args.run_dir),
            profile=args.profile,
            gateway_command=shlex.split(args.gateway_cmd),
            access=c.model.claude_code_access,
            timeout=float(args.timeout),
            claude_bin=shutil.which(claude_run.CLAUDE_BIN),
        )
    except (ProviderNotAllowed, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if result.timed_out:
        print(f"run stopped at the time limit; output in {result.output_path}", file=sys.stderr)
        return 124
    print(f"run finished (exit {result.exit_code}); output in {result.output_path}")
    if result.final:
        print(f"result: {str(result.final.get('result', ''))[:500]}")
    return 0 if result.exit_code == 0 else 1


def cmd_ask(args) -> int:
    import os

    from wf.model import ModelRequest, Message, build_model
    from wf.model.recording import record_call
    from wf.usage import UsageLedger

    c = cfgmod.load(_cfg_path(args))
    model = build_model(c.model, env=dict(os.environ))
    request = ModelRequest(
        system=args.system or "",
        messages=(Message("user", args.prompt),),
        max_tokens=args.max_tokens,
    )
    resp = model.complete(request)
    ledger = UsageLedger(Path(args.db or os.environ.get("WF_DB", "wf.sqlite")))
    record_call(ledger, args.agent, resp, ticket=args.ticket, provider=model.name)
    print(resp.text)
    print(f"\n[{model.name} {resp.model}: {resp.input_tokens} in, {resp.output_tokens} out, "
          f"{resp.cache_read_tokens} cache-read, {resp.duration_ms} ms]", file=sys.stderr)
    return 0


def cmd_doctor(args) -> int:
    from wf import preflight

    c = cfgmod.load(_cfg_path(args))
    checks = preflight.run_checks(claude_code_access=c.model.claude_code_access)
    print(preflight.format_checks(checks))
    failed = [ch for ch in checks if ch.failed]
    if failed:
        print(f"\n{len(failed)} check(s) failed.", file=sys.stderr)
        return 1
    print("\nall required checks passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wf", description="Agentic workflow shell")
    p.add_argument("--config", help="path to workflow.json (default: $WF_CONFIG or ./workflow.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    link = sub.add_parser("link", help="link a repo or board")
    lsub = link.add_subparsers(dest="what", required=True)
    lr = lsub.add_parser("repo", help="link an internal repo (a local git repository)")
    lr.add_argument("name", help="lowercase name, e.g. api")
    lr.add_argument("repo_path", metavar="PATH", help="path to the local git repository")
    lr.set_defaults(func=cmd_link_repo)
    lb = lsub.add_parser("board", help="link a Jira board")
    lb.add_argument("provider", choices=cfgmod.BOARD_PROVIDERS)
    lb.add_argument("ref", help="numeric Jira board id")
    lb.add_argument("--label", help="a name to show in `wf list`")
    lb.set_defaults(func=cmd_link_board)

    un = sub.add_parser("unlink", help="remove a linked repo or board")
    un.add_argument("kind", choices=["repo", *cfgmod.BOARD_PROVIDERS])
    un.add_argument("ref")
    un.set_defaults(func=cmd_unlink)

    ls = sub.add_parser("list", help="show linked repos and boards")
    ls.set_defaults(func=cmd_list)

    ck = sub.add_parser("check", help="verify every link resolves with current credentials")
    ck.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="start a workflow run (shell stages only)")
    run.add_argument("request")
    run.add_argument("--thread", required=True, help="run id, reused by `wf resume`")
    run.add_argument("--db", default="runs.sqlite")
    run.set_defaults(func=cmd_run)

    us = sub.add_parser("usage", help="audit token use per agent from the local ledger")
    us.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    us.set_defaults(func=cmd_usage)

    rt = sub.add_parser("route", help="show which agent owns a ticket status")
    rt.add_argument("status")
    rt.add_argument("--hotfix", action="store_true", help="treat the ticket as carrying the hotfix label")
    rt.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    rt.set_defaults(func=cmd_route)

    sy = sub.add_parser("sync", help="apply a sync payload from the Claude session (see docs/sync-format.md)")
    sy.add_argument("from_file", metavar="FILE", help="JSON payload")
    sy.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    sy.set_defaults(func=cmd_sync)

    bd = sub.add_parser("board", help="show the active sprint, grouped by stage")
    bd.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    bd.set_defaults(func=cmd_board)

    bl = sub.add_parser("backlog", help="show the backlog (tickets in no sprint), grouped by stage")
    bl.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    bl.set_defaults(func=cmd_backlog)

    ini = sub.add_parser("init", help="set up this machine: connect the MCP, then pick the repos you work on")
    ini.add_argument("--mcp-connected", action="store_true", help="say the Atlassian MCP is already connected")
    ini.add_argument("--root", action="append", default=[], metavar="FOLDER",
                     help="folder to look in for git repos (repeat for more)")
    ini.add_argument("--pick", default=None, metavar="N,N,...", help="numbers of the repos to link")
    ini.set_defaults(func=cmd_init)

    ob = sub.add_parser("outbox", help="moves waiting to be pushed to Jira (used by the Jira agent)")
    ob_sub = ob.add_subparsers(dest="outbox_cmd", required=True)
    ob_list = ob_sub.add_parser("list", help="every outbox item, as JSON")
    ob_list.add_argument("--status", default=None)
    ob_pending = ob_sub.add_parser("pending", help="items waiting to be planned, as JSON")
    ob_plan = ob_sub.add_parser("plan", help="decide what to send, given the observed Jira state (JSON)")
    ob_plan.add_argument("from_file", metavar="FILE")
    ob_ack = ob_sub.add_parser("ack", help="record the results of the sends (JSON)")
    ob_ack.add_argument("from_file", metavar="FILE")
    ob_retry = ob_sub.add_parser("retry", help="put a blocked, conflicted, failed, or stuck item back to pending")
    ob_retry.add_argument("id", type=int)
    for outbox_parser in (ob_list, ob_pending, ob_plan, ob_ack, ob_retry):
        outbox_parser.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    ob.set_defaults(func=cmd_outbox)

    st = sub.add_parser("status", help="when the board last synced, what is in scope, and what is blocked")
    st.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    st.set_defaults(func=cmd_status)

    ag = sub.add_parser("agents", help="run the agents: tick over the board, merge, or publish")
    ag_sub = ag.add_subparsers(dest="agents_cmd", required=True)
    at = ag_sub.add_parser("tick", help="run the role for each ticket that needs one (dry run unless --apply)")
    at.add_argument("--apply", action="store_true")
    at.add_argument("--repo", default=None, help="linked internal repo name")
    at.add_argument("--runs", default=None, help="directory for worktrees and run output (default: config)")
    at.add_argument("--test-cmd", default=None, help="shell command that runs the project's tests (default: config)")
    at.add_argument("--model", default=None)
    at.add_argument("--db", default=None)
    at.add_argument("--config", default=None)
    at.set_defaults(func=cmd_agents_tick)
    am = ag_sub.add_parser("merge", help="merge an approved ticket into the internal main")
    am.add_argument("key")
    am.add_argument("--repo", default=None)
    am.add_argument("--runs", default=None)
    am.add_argument("--test-cmd", default=None, help="default: config")
    am.add_argument("--db", default=None)
    am.add_argument("--config", default=None)
    am.set_defaults(func=cmd_agents_merge)
    ap = ag_sub.add_parser("publish", help="open a real GitHub PR for a fully passed ticket (needs --confirm)")
    ap.add_argument("key")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--config", default=None)
    ap.set_defaults(func=cmd_agents_publish)

    bt = sub.add_parser("bots", help="bot accounts: set them up, or see what they may do")
    bt_sub = bt.add_subparsers(dest="bots_cmd", required=True)
    bs = bt_sub.add_parser("setup", help="ask whether to create bot credentials, then create them")
    bs.add_argument("--domain", default=None, help="accounts are <role>@<domain> (default: fokist.com)")
    bs.add_argument("--yes", action="store_true", help="skip the question and create them")
    bs.add_argument("--config", default=None)
    bs.set_defaults(func=cmd_bots_setup)
    bp = bt_sub.add_parser("permissions", help="show what the bots may and may not do")
    bp.set_defaults(func=cmd_bots_permissions)

    ev = sub.add_parser("env", help="local environment: start or stop a project's app for the bots and you")
    ev.add_argument("env_cmd", choices=["up", "down", "status"])
    ev.add_argument("--repo", default=None, help="linked repo to run (up)")
    ev.add_argument("--name", default=None, help="environment name (default: app)")
    ev.add_argument("--config", default=None)
    ev.set_defaults(func=cmd_env)

    pk = sub.add_parser("pickup", help="dev pickup: move ready tickets to in_dev (dry run unless --apply)")
    pk.add_argument("--apply", action="store_true", help="actually move the tickets")
    pk.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    pk.set_defaults(func=cmd_pickup)

    mv = sub.add_parser("move", help="move a ticket to an internal stage (the harness or an agent role)")
    mv.add_argument("key")
    mv.add_argument("stage")
    mv.add_argument("--actor", required=True, help="who is moving it, e.g. dev, qa, user")
    mv.add_argument("--reason", required=True)
    mv.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    mv.set_defaults(func=cmd_move)

    fl = sub.add_parser("flag", help="flag a ticket for a human, or clear a flag (user only)")
    fl.add_argument("key")
    fl.add_argument("flag", choices=["needs-human", "blocked"])
    fl.add_argument("--actor", required=True)
    fl.add_argument("--reason", default=None)
    fl.add_argument("--clear", action="store_true", help="clear the flag instead of setting it")
    fl.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    fl.set_defaults(func=cmd_flag)

    sp = sub.add_parser("sprints", help="list sprints")
    sp.add_argument("--state", choices=["future", "active", "closed"], default=None)
    sp.add_argument("--db", default=None, help="SQLite file (default: $WF_DB or ./wf.sqlite)")
    sp.set_defaults(func=cmd_sprints)

    kl = sub.add_parser("kill", help="kill switch: stop all agent dispatch")
    kl.add_argument("state", choices=["on", "off", "status"])
    kl.add_argument("--db", default=None)
    kl.set_defaults(func=cmd_kill)

    pl = sub.add_parser("poll", help="hourly fallback: pull recent Jira changes and route them")
    pl.add_argument("--db", default=None)
    pl.set_defaults(func=cmd_poll)

    res = sub.add_parser("resume", help="answer the approval gate for a run")
    res.add_argument("thread")
    res.add_argument("--db", default="runs.sqlite")
    g = res.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", dest="approve", action="store_false")
    res.set_defaults(func=cmd_resume)

    md = sub.add_parser("model", help="show or set the model engine")
    msub = md.add_subparsers(dest="model_cmd", required=True)
    ms = msub.add_parser("status", help="show provider, model, and whether credentials are set")
    ms.set_defaults(func=cmd_model_status)
    mset = msub.add_parser("set", help="change the provider or model in workflow.json")
    mset.add_argument("--provider", help="ollama, anthropic-api, or claude-code")
    mset.add_argument("--model")
    mset.add_argument("--base-url", dest="base_url")
    mset.add_argument("--api-key-env", dest="api_key_env", help="env var that holds the key")
    access = mset.add_mutually_exclusive_group()
    access.add_argument("--enable-claude-code", dest="claude_code_access", action="store_const", const=True,
                        help="allow Claude Code runs on this machine (operator-provisioned only)")
    access.add_argument("--disable-claude-code", dest="claude_code_access", action="store_const", const=False)
    mset.set_defaults(func=cmd_model_set)

    rc = sub.add_parser("run-claude", help="start one Claude Code run in a ticket's worktree")
    rc.add_argument("--worktree", required=True, help="the ticket's git worktree")
    rc.add_argument("--prompt-file", dest="prompt_file", required=True, help="ticket text for this run")
    rc.add_argument("--system-prompt-file", dest="system_prompt_file", required=True,
                    help="the agent's prompt, e.g. prompts/dev.md")
    rc.add_argument("--run-dir", dest="run_dir", required=True, help="where this run's files are kept")
    rc.add_argument("--profile", default="dev", help="what the run may do (see wf.claude_run.PROFILES)")
    rc.add_argument("--gateway-cmd", dest="gateway_cmd", required=True,
                    help="command that starts the tool gateway's MCP server")
    rc.add_argument("--model", help="model name (default: the configured model)")
    rc.add_argument("--timeout", default="3600", help="seconds before the run is stopped")
    rc.set_defaults(func=cmd_run_claude)

    ask = sub.add_parser("ask", help="send one prompt through the configured model engine")
    ask.add_argument("prompt")
    ask.add_argument("--agent", default="manual", help="agent name recorded in the ledger")
    ask.add_argument("--ticket", help="ticket key recorded in the ledger")
    ask.add_argument("--system", help="system prompt")
    ask.add_argument("--max-tokens", dest="max_tokens", type=int, default=1024)
    ask.add_argument("--db", default=None, help="ledger file (default: $WF_DB or ./wf.sqlite)")
    ask.set_defaults(func=cmd_ask)

    doc = sub.add_parser("doctor", help="check that this Mac is ready to run the Claude Code flow")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except cfgmod.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
