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

# Requests to a remote app carry this header. An edge firewall can use it to let the runner through
# its browser checks when the request carries the matching secret. The firewall is configured outside this tool.
RUNNER_SECRET_HEADER = "X-Wf-Runner-Secret"


def runner_headers(url: str, role: str, secret: str, secret_host: str) -> dict[str, str]:
    """Headers for a request the runner makes to the app.

    The User-Agent names the bot, so it can be told apart from a browser and from Python's default.
    The secret goes only to secret_host (the dev deploy). Any other host, including prod and the
    local app, never sees it.
    """
    import urllib.parse

    headers = {"User-Agent": f"wf-{role or 'agent'}-bot/1.0"}
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if secret and secret_host and host == secret_host.lower():
        headers[RUNNER_SECRET_HEADER] = secret
    return headers


# Sign-up pages create accounts. QA never needs them, so fetch_local refuses them.
SIGNUP_SEGMENTS = ("register", "signup", "sign-up", "sign_up")

# fetch_local returns this many bytes of a body. Big enough for a stylesheet or a page, so QA can read
# the whole thing (a media query near the end of a CSS file used to be cut off at 4,000 bytes).
MAX_FETCH_BYTES = 512 * 1024


def is_signup_path(path: str) -> bool:
    """True when a path points at a sign-up page.

    Decodes percent-escapes and resolves `..` first, so /%72egister and /login/../register
    are caught as well as /register/ and /register?x=1.
    """
    import posixpath
    from urllib.parse import unquote

    clean = unquote(path.split("?", 1)[0].split("#", 1)[0])
    normal = posixpath.normpath("/" + clean.lstrip("/"))
    first = normal.strip("/").split("/", 1)[0].lower()
    return first in SIGNUP_SEGMENTS

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

    def link_blocking(self, args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            board.add_block(conn, args["prerequisite"], self.scope.key, self.scope.role, args["reason"])
        finally:
            conn.close()
        return {"linked": args["prerequisite"]}

    def set_order(self, args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            board.set_order(conn, self.scope.key, int(args["position"]), self.scope.role)
        finally:
            conn.close()
        return {"position": int(args["position"])}

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

    def request_perf_check(self, args: dict) -> dict:
        conn = board.connect(self.scope.db)
        try:
            board.request_perf_check(conn, self.scope.key, self.scope.role, args["reason"])
        finally:
            conn.close()
        return {"requested": True}

    def approve_ticket(self, _args: dict) -> dict:
        head = self._head()
        if self.scope.role == "pr_review" and self.scope.worktree is not None:
            from wf import merge_bot
            ok, stray = merge_bot.stray_commits(self.scope.key, self.scope.worktree)
            if not ok:
                raise PermissionError(
                    f"not approving: the branch would also add commits that are not {self.scope.key}, "
                    f"starting with {stray[0][:70]!r}. Use request_changes and say which commits to remove.")
        conn = board.connect(self.scope.db)
        try:
            board.record_approval(conn, self.scope.key, self.scope.role, head)
            stage = conn.execute("select stage from ticket where key = ?", (self.scope.key,)).fetchone()["stage"]
            if self.scope.role == "pr_review" and stage in ("pr_open", "pr_in_review"):
                board.move_stage(conn, self.scope.key, "ready_for_perf_review", self.scope.role,
                                 f"PR review approved at {head[:8]}")
            if self.scope.role == "perf_review" and stage == "ready_for_perf_review":
                board.move_stage(conn, self.scope.key, "ready_for_qa", self.scope.role,
                                 f"code performance review approved at {head[:8]}")
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

    _external: str | None = None

    def start_app(self, _args: dict) -> dict:
        import socket
        import time
        import urllib.request

        from wf import envs

        if self.scope.app_url and envs.answers(self.scope.app_url):
            self._external = self.scope.app_url.rstrip("/")
            return {"url": self._external, "running": "already running"}
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

        if self._external is not None:
            base = self._external
        elif self._app is None or self._app.poll() is not None or self._port is None:
            raise ValueError("the app is not running; call start_app first")
        else:
            base = f"http://127.0.0.1:{self._port}"
        path = args["path"] if args["path"].startswith("/") else "/" + args["path"]
        if is_signup_path(path):
            raise ValueError("sign-up pages are not part of QA; the bot only signs in with bot_login")
        url = f"{base}{path}"
        req = urllib.request.Request(url, headers=self._runner_headers(url))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {"status": resp.status, "body": resp.read(MAX_FETCH_BYTES).decode(errors="replace")}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": e.read(MAX_FETCH_BYTES).decode(errors="replace")}

    def stop_app(self, _args: dict) -> dict:
        self._external = None  # an app we only borrowed is never stopped
        if self._app is not None and self._app.poll() is None:
            self._app.terminate()
            try:
                self._app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._app.kill()
        self._app = None
        return {"stopped": True}

    _bot_jar = None

    def _bot_password(self) -> str:
        path = self.scope.bot_credentials
        if path is None or not path.exists():
            raise ValueError("no credentials file is configured for this run")
        want = f"{self.scope.role}_password"
        for line in path.read_text().splitlines():
            if "=" in line and line.split("=", 1)[0].strip().lower() == want:
                return line.split("=", 1)[1]
        raise ValueError(f"no password for the {self.scope.role} bot in the credentials file")

    def _bot_opener(self):
        import http.cookiejar
        import urllib.request

        if self._bot_jar is None:
            self._bot_jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None  # keep the 303 so the login result is visible

        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._bot_jar), NoRedirect)

    def _runner_headers(self, url: str) -> dict[str, str]:
        return runner_headers(url, self.scope.role, self.scope.runner_secret, self.scope.runner_secret_host)

    def _bot_form(self, path: str, form: dict) -> tuple[int, str, str]:
        import urllib.error
        import urllib.parse
        import urllib.request

        data = urllib.parse.urlencode(form).encode()
        target = self.scope.bot_app.rstrip("/") + path
        req = urllib.request.Request(target, data=data, method="POST",
                                     headers=self._runner_headers(target))
        try:
            with self._bot_opener().open(req, timeout=20) as resp:
                return resp.status, resp.headers.get("Location", ""), ""
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", ""), ""

    def bot_login(self, _args: dict) -> dict:
        import urllib.error

        if not self.scope.bot_email:
            raise ValueError("this run has no bot account")
        password = self._bot_password()
        status, location, _ = self._bot_form("/login", {"email": self.scope.bot_email, "password": password})
        if status != 303 or location.startswith("/login"):
            raise PermissionError(f"login failed for {self.scope.bot_email} (HTTP {status})")
        # The session cookie must open a page that needs a login.
        try:
            target = self.scope.bot_app.rstrip("/") + "/search"
            req = urllib.request.Request(target, headers=self._runner_headers(target))
            with self._bot_opener().open(req, timeout=20) as resp:
                if resp.status != 200:
                    raise PermissionError("logged in, but the session is not accepted")
        except urllib.error.HTTPError as e:
            raise PermissionError(f"logged in, but the session check failed (HTTP {e.code})")
        return {"logged_in": True, "email": self.scope.bot_email}

    def bot_logout(self, _args: dict) -> dict:
        status, _, _ = self._bot_form("/logout", {})
        self._bot_jar = None
        return {"logged_out": status in (200, 303)}

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
