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


def cmd_route(args) -> int:
    from wf import orchestrator

    labels = {"hotfix"} if args.hotfix else set()
    d = orchestrator.route(args.status, labels)
    print(f"action: {d.action}\nowner:  {d.owner or '-'}\nwhy:    {d.reason}")
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
    rt.set_defaults(func=cmd_route)

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
