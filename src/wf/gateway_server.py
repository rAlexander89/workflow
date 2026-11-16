"""Gateway: the MCP server every agent run connects to. It is the only way an agent
reaches the board or its repo.

One process serves one run. The run's role, ticket key, and worktree come from the
environment when the process starts, so an agent can't change them per call:

    WF_ROLE       the role this run plays (dev, qa, pr_review, ...)
    WF_KEY        the one ticket this run may touch
    WF_DB         the board's SQLite file
    WF_WORKTREE   the git worktree this run may change
    WF_TEST_CMD   the shell command that runs the project's tests (optional)

Tools are the ones claude_run's PROFILES name: get_ticket, move_ticket, flag_ticket,
run_tests, commit, open_pr. open_pr is refused: the GitHub agent is the only component
allowed to open a real pull request, and it runs after the internal pipeline passes.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from wf import board

PROTOCOL = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {"name": "get_ticket", "description": "Read the ticket this run is working on.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "move_ticket", "description": "Move the ticket to an internal stage allowed for this role.",
     "inputSchema": {"type": "object", "required": ["stage", "reason"],
                     "properties": {"stage": {"type": "string"}, "reason": {"type": "string"}}}},
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
        if not self.role or not self.key:
            raise ValueError("WF_ROLE and WF_KEY are required")


class Gateway:
    def __init__(self, scope: Scope):
        self.scope = scope

    # --- tools -------------------------------------------------------------------
    def get_ticket(self, _args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            detail = board.ticket_detail(conn, self.scope.key)
        finally:
            conn.close()
        if detail is None:
            raise board.BoardError(f"no ticket {self.scope.key}")
        return detail

    def move_ticket(self, args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            board.move_stage(conn, self.scope.key, args["stage"], self.scope.role, args["reason"])
        finally:
            conn.close()
        return {"moved_to": args["stage"]}

    def flag_ticket(self, args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            board.set_flag(conn, self.scope.key, args["flag"], self.scope.role, args["reason"])
        finally:
            conn.close()
        return {"flagged": args["flag"]}

    def run_tests(self, _args: dict) -> dict:
        if not self.scope.test_cmd or self.scope.worktree is None:
            raise ValueError("no test command is configured for this run")
        proc = subprocess.run(shlex.split(self.scope.test_cmd), cwd=self.scope.worktree,
                              capture_output=True, text=True, timeout=900, check=False)
        return {"exit_code": proc.returncode, "output_tail": (proc.stdout + proc.stderr)[-4000:]}

    def commit(self, args: dict) -> dict:
        wt = self.scope.worktree
        if wt is None:
            raise ValueError("no worktree for this run")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        proc = subprocess.run(["git", "commit", "-m", args["message"]], cwd=wt,
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
            raise RuntimeError(proc.stderr.strip() or "git commit failed")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True,
                             text=True, check=True).stdout.strip()
        return {"commit": sha}

    MAX_COMMENT = 600

    def post_comment(self, args: dict) -> dict:
        body = args["body"].strip()
        if len(body) > self.MAX_COMMENT:
            raise ValueError(f"comment is {len(body)} characters; keep it under {self.MAX_COMMENT}. "
                             "Lead with the decision or blocker, then one line per open question.")
        conn = board.connect(self.scope.db)
        try:
            cid = board.add_comment(conn, self.scope.key, self.scope.role, args["body"])
        finally:
            conn.close()
        return {"comment_id": cid}

    def _head(self) -> str:
        if self.scope.worktree is None:
            raise ValueError("no worktree for this run")
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.scope.worktree,
                              capture_output=True, text=True, check=True).stdout.strip()

    def approve_ticket(self, _args: dict) -> dict:
        head = self._head()
        conn = board.connect(self.scope.db)
        try:
            board.record_approval(conn, self.scope.key, self.scope.role, head)
        finally:
            conn.close()
        return {"approved": head}

    def request_changes(self, args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            board.move_stage(conn, self.scope.key, "in_dev", self.scope.role, args["reason"])
            board.add_comment(conn, self.scope.key, self.scope.role, args["reason"])
        finally:
            conn.close()
        return {"moved_to": "in_dev"}

    # --- the running app ----------------------------------------------------------
    _app: subprocess.Popen | None = None
    _port: int | None = None

    def start_app(self, _args: dict) -> dict:
        import socket
        import time
        import urllib.request

        if not self.scope.app_cmd or self.scope.worktree is None:
            raise ValueError("no app command is configured for this project")
        if self._app is not None and self._app.poll() is None:
            return {"url": f"http://127.0.0.1:{self._port}"}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cmd = self.scope.app_cmd.replace("{port}", str(port))
        self._app = subprocess.Popen(shlex.split(cmd), cwd=self.scope.worktree,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._port = port
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._app.poll() is not None:
                raise RuntimeError(f"the app exited during startup (code {self._app.returncode})")
            try:
                urllib.request.urlopen(url + "/", timeout=2).close()
                return {"url": url}
            except Exception:
                time.sleep(0.5)
        self.stop_app({})
        raise RuntimeError("the app did not answer within 60 seconds")

    def fetch_local(self, args: dict) -> dict:
        import urllib.error
        import urllib.request

        if self._app is None or self._app.poll() is not None or self._port is None:
            raise ValueError("the app is not running; call start_app first")
        path = args["path"] if args["path"].startswith("/") else "/" + args["path"]
        url = f"http://127.0.0.1:{self._port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return {"status": resp.status, "body": resp.read(4000).decode(errors="replace")}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": e.read(4000).decode(errors="replace")}

    def stop_app(self, _args: dict) -> dict:
        if self._app is not None and self._app.poll() is None:
            self._app.terminate()
            try:
                self._app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._app.kill()
        self._app = None
        return {"stopped": True}

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
