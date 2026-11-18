"""Gateway: the MCP server every agent run connects to. It is the only way an agent
reaches the board or its repo.

One process serves one run. The run's role, ticket key, and worktree come from the
environment when the process starts, so an agent can't change them per call:

    WF_ROLE       the role this run plays (dev, qa, pr_review, ...)
    WF_KEY        the one ticket this run may touch
    WF_DB         the board's SQLite file
    WF_WORKTREE   the git worktree this run may change
    WF_TEST_CMD   the shell command that runs the project's tests (optional)

Tools are the ones the Claude Code profiles name: get_ticket, move_ticket, flag_ticket,
run_tests, commit, open_pr. open_pr is refused: the GitHub agent is the only component
allowed to open a real pull request, and it runs after the internal pipeline passes.

This module only speaks the protocol. What each tool does is `RunTools` in the application layer.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from wf.application.execution.gateway import RunTools
from wf.bootstrap import Container
from wf.domain.execution.gateway_rules import (  # noqa: F401  (the gateway's public helpers)
    MAX_FETCH_BYTES, RUNNER_SECRET_HEADER, is_signup_path, runner_headers,
)

PROTOCOL = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {"name": "get_ticket", "description": "Read the ticket this run is working on.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "move_ticket", "description": "Move the ticket to an internal stage allowed for this role.",
     "inputSchema": {"type": "object", "required": ["stage", "reason"],
                     "properties": {"stage": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "link_blocking", "description": "Staff only: say that this ticket must wait for another ticket to merge to dev. Adds a Blocks link in Jira.",
     "inputSchema": {"type": "object", "required": ["prerequisite", "reason"],
                     "properties": {"prerequisite": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "set_order", "description": "Staff only: this ticket's place in the sprint's work order. Lower goes first.",
     "inputSchema": {"type": "object", "required": ["position"],
                     "properties": {"position": {"type": "integer"}}}},
    {"name": "flag_ticket", "description": "Flag the ticket for a human (needs-human or blocked).",
     "inputSchema": {"type": "object", "required": ["flag", "reason"],
                     "properties": {"flag": {"type": "string", "enum": ["needs-human", "blocked"]},
                                    "reason": {"type": "string"}}}},
    {"name": "run_tests", "description": "Run the project's test command in this run's worktree.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "commit", "description": "Commit the worktree's changes to the run's branch.",
     "inputSchema": {"type": "object", "required": ["message"],
                     "properties": {"message": {"type": "string"}}}},
    {"name": "post_comment", "description": "Comment on the ticket. Queued for Jira by the outbox.",
     "inputSchema": {"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}}}},
    {"name": "approve_ticket", "description": "Approve the ticket at the worktree's current commit.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "request_changes", "description": "Send the ticket back to dev with a reason.",
     "inputSchema": {"type": "object", "required": ["reason"], "properties": {"reason": {"type": "string"}}}},
    {"name": "start_app", "description": "Start the project's app from this worktree on a local port. Returns its URL.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fetch_local", "description": "GET a path from the running app (start_app first).",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
    {"name": "stop_app", "description": "Stop the app started by start_app.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "bot_login", "description": "Log in to the local app as this run's bot account. The password is read here and never returned.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "bot_logout", "description": "Log out of the local app.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "request_perf_check", "description": "Dev only: ask for a performance check on this ticket, with the reason.",
     "inputSchema": {"type": "object", "required": ["reason"], "properties": {"reason": {"type": "string"}}}},
    {"name": "open_pr", "description": "Not available to agents. Refused.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Scope:
    def __init__(self, env: dict[str, str] | None = None):
        env = dict(os.environ if env is None else env)
        self.role = env.get("WF_ROLE", "")
        self.key = env.get("WF_KEY", "")
        self.db = Path(env.get("WF_DB", "wf.sqlite"))
        self.worktree = Path(env["WF_WORKTREE"]) if env.get("WF_WORKTREE") else None
        self.test_cmd = env.get("WF_TEST_CMD", "")
        self.app_cmd = env.get("WF_APP_CMD", "")  # the project's own command to start its app; "{port}" is replaced
        self.app_url = env.get("WF_APP_URL", "")  # an app already running; used instead of starting one
        self.bot_email = env.get("WF_BOT_EMAIL", "")  # this run's app account
        self.bot_credentials = Path(env["WF_BOT_CREDENTIALS"]) if env.get("WF_BOT_CREDENTIALS") else None
        self.bot_app = env.get("WF_BOT_APP", "http://127.0.0.1:3002")  # the local app; bots sign in through its login form
        self.runner_secret = env.get("WF_RUNNER_SECRET", "")  # sent to runner_secret_host only; see runner_headers
        self.runner_secret_host = env.get("WF_RUNNER_SECRET_HOST", "")  # the dev deploy's hostname, nothing else
        if not self.role or not self.key:
            raise ValueError("WF_ROLE and WF_KEY are required")


class Gateway:
    MAX_COMMENT = 600

    def __init__(self, scope: Scope):
        self.scope = scope
        self._container = Container(scope.db, Path(os.environ.get("WF_CONFIG", "workflow.json")))
        self._toolbox = self._container.run_toolbox(worktree=scope.worktree, key=scope.key, test_cmd=scope.test_cmd)
        self._app = self._container.app_session(
            role=scope.role, worktree=scope.worktree, app_cmd=scope.app_cmd, app_url=scope.app_url,
            bot_email=scope.bot_email, bot_credentials=scope.bot_credentials, bot_app=scope.bot_app,
            runner_secret=scope.runner_secret, runner_secret_host=scope.runner_secret_host)

    def _run(self, call):
        """Call a tool with this run's scope on a fresh board session."""
        with self._container.session() as s:
            return call(RunTools(role=self.scope.role, key=self.scope.key, commands=s.tickets, queries=s.queries,
                                 toolbox=self._toolbox, app=self._app))

    # --- tools -------------------------------------------------------------------
    def get_ticket(self, _args: dict) -> dict:
        return self._run(lambda t: t.get_ticket())

    def move_ticket(self, args: dict) -> dict:
        return self._run(lambda t: t.move_ticket(args["stage"], args["reason"]))

    def link_blocking(self, args: dict) -> dict:
        return self._run(lambda t: t.link_blocking(args["prerequisite"], args["reason"]))

    def set_order(self, args: dict) -> dict:
        return self._run(lambda t: t.set_order(int(args["position"])))

    def flag_ticket(self, args: dict) -> dict:
        return self._run(lambda t: t.flag_ticket(args["flag"], args["reason"]))

    def run_tests(self, _args: dict) -> dict:
        return self._run(lambda t: t.run_tests())

    def commit(self, args: dict) -> dict:
        return self._run(lambda t: t.commit(args["message"]))

    def post_comment(self, args: dict) -> dict:
        return self._run(lambda t: t.post_comment(args["body"]))

    def request_perf_check(self, args: dict) -> dict:
        return self._run(lambda t: t.request_perf_check(args["reason"]))

    def approve_ticket(self, _args: dict) -> dict:
        return self._run(lambda t: t.approve_ticket())

    def request_changes(self, args: dict) -> dict:
        return self._run(lambda t: t.request_changes(args["reason"]))

    # --- the running app ----------------------------------------------------------
    def start_app(self, _args: dict) -> dict:
        return self._run(lambda t: t.start_app())

    def fetch_local(self, args: dict) -> dict:
        return self._run(lambda t: t.fetch_local(args["path"]))

    def stop_app(self, _args: dict) -> dict:
        return self._run(lambda t: t.stop_app())

    def bot_login(self, _args: dict) -> dict:
        return self._run(lambda t: t.bot_login())

    def bot_logout(self, _args: dict) -> dict:
        return self._run(lambda t: t.bot_logout())

    def open_pr(self, _args: dict) -> dict:
        raise PermissionError("agents may not open pull requests; the GitHub agent does this "
                              "after the internal pipeline passes")

    # --- protocol ----------------------------------------------------------------
    def call(self, name: str, args: dict) -> dict:
        handler = {t["name"]: getattr(self, t["name"]) for t in TOOLS}.get(name)
        if handler is None:
            raise KeyError(f"unknown tool {name!r}")
        return handler(args)

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "wf-gateway", "version": "0.1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                out = self.call(params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(out, default=str)}], "isError": False}
            except Exception as e:  # tool errors go back to the agent, not the process
                result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        elif method and method.startswith("notifications/"):
            return None
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout, scope: Scope | None = None) -> None:
    gw = Gateway(scope or Scope())
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            reply = gw.handle(json.loads(line))
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
    finally:
        gw.stop_app({})


if __name__ == "__main__":
    serve()
