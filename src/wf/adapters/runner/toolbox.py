"""The tools an agent run uses on its own machine: its git worktree, the project's app, and the app's login."""

from __future__ import annotations

import http.cookiejar
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from wf.adapters.env import local_app
from wf.adapters.workspace.git import GitWorkspace
from wf.domain.execution.gateway_rules import MAX_FETCH_BYTES, bot_password, runner_headers


class WorktreeToolbox:
    def __init__(self, *, worktree: Path | None, key: str, test_cmd: str = ""):
        self._worktree = worktree
        self._key = key
        self._test_cmd = test_cmd

    def head(self) -> str:
        if self._worktree is None:
            raise ValueError("no worktree for this run")
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self._worktree, capture_output=True, text=True,
                              check=True).stdout.strip()

    def stray_commits(self) -> tuple[bool, list[str]]:
        return GitWorkspace(self._worktree).stray_commits(self._key)

    def run_tests(self) -> dict:
        if not self._test_cmd or self._worktree is None:
            raise ValueError("no test command is configured for this run")
        proc = subprocess.run(shlex.split(self._test_cmd), cwd=self._worktree, capture_output=True, text=True,
                              timeout=900, check=False)
        return {"exit_code": proc.returncode, "output_tail": (proc.stdout + proc.stderr)[-4000:]}

    def commit(self, message: str) -> str:
        wt = self._worktree
        if wt is None:
            raise ValueError("no worktree for this run")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        proc = subprocess.run(["git", "commit", "-m", message], cwd=wt, capture_output=True, text=True, check=False)
        if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
            raise RuntimeError(proc.stderr.strip() or "git commit failed")
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True,
                              check=True).stdout.strip()


@dataclass(frozen=True)
class AppSettings:
    role: str
    worktree: Path | None = None
    app_cmd: str = ""  # the project's own command to start its app; "{port}" is replaced
    app_url: str = ""  # an app already running; used instead of starting one
    bot_email: str = ""
    bot_credentials: Path | None = None
    bot_app: str = "http://127.0.0.1:3002"  # the local app; bots sign in through its login form
    runner_secret: str = ""  # sent to runner_secret_host only
    runner_secret_host: str = ""  # the dev deploy's hostname, nothing else


class LocalAppSession:
    """The app as one run uses it. Holds the process it started and the bot's session cookies."""

    def __init__(self, settings: AppSettings):
        self._s = settings
        self._app: subprocess.Popen | None = None
        self._port: int | None = None
        self._external: str | None = None
        self._bot_jar = None

    def _headers(self, url: str) -> dict[str, str]:
        return runner_headers(url, self._s.role, self._s.runner_secret, self._s.runner_secret_host)

    def start_app(self) -> dict:
        s = self._s
        if s.app_url and local_app.answers(s.app_url):
            self._external = s.app_url.rstrip("/")
            return {"url": self._external, "running": "already running"}
        if not s.app_cmd or s.worktree is None:
            raise ValueError("no app command is configured for this project")
        if self._app is not None and self._app.poll() is None:
            return {"url": f"http://127.0.0.1:{self._port}"}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cmd = s.app_cmd.replace("{port}", str(port))
        self._app = subprocess.Popen(shlex.split(cmd), cwd=s.worktree,
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
        self.stop_app()
        raise RuntimeError("the app did not answer within 60 seconds")

    def fetch_local(self, path: str) -> dict:
        if self._external is not None:
            base = self._external
        elif self._app is None or self._app.poll() is not None or self._port is None:
            raise ValueError("the app is not running; call start_app first")
        else:
            base = f"http://127.0.0.1:{self._port}"
        url = f"{base}{path}"
        req = urllib.request.Request(url, headers=self._headers(url))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {"status": resp.status, "body": resp.read(MAX_FETCH_BYTES).decode(errors="replace")}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": e.read(MAX_FETCH_BYTES).decode(errors="replace")}

    def stop_app(self) -> dict:
        self._external = None  # an app we only borrowed is never stopped
        if self._app is not None and self._app.poll() is None:
            self._app.terminate()
            try:
                self._app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._app.kill()
        self._app = None
        return {"stopped": True}

    # --- the bot's login --------------------------------------------------------------------

    def _password(self) -> str:
        path = self._s.bot_credentials
        if path is None or not path.exists():
            raise ValueError("no credentials file is configured for this run")
        return bot_password(path.read_text(), self._s.role)

    def _opener(self):
        if self._bot_jar is None:
            self._bot_jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None  # keep the 303 so the login result is visible

        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._bot_jar), NoRedirect)

    def _form(self, path: str, form: dict) -> tuple[int, str]:
        data = urllib.parse.urlencode(form).encode()
        target = self._s.bot_app.rstrip("/") + path
        req = urllib.request.Request(target, data=data, method="POST", headers=self._headers(target))
        try:
            with self._opener().open(req, timeout=20) as resp:
                return resp.status, resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", "")

    def bot_login(self) -> dict:
        s = self._s
        if not s.bot_email:
            raise ValueError("this run has no bot account")
        status, location = self._form("/login", {"email": s.bot_email, "password": self._password()})
        if status != 303 or location.startswith("/login"):
            raise PermissionError(f"login failed for {s.bot_email} (HTTP {status})")
        # The session cookie must open a page that needs a login.
        try:
            target = s.bot_app.rstrip("/") + "/search"
            req = urllib.request.Request(target, headers=self._headers(target))
            with self._opener().open(req, timeout=20) as resp:
                if resp.status != 200:
                    raise PermissionError("logged in, but the session is not accepted")
        except urllib.error.HTTPError as e:
            raise PermissionError(f"logged in, but the session check failed (HTTP {e.code})")
        return {"logged_in": True, "email": s.bot_email}

    def bot_logout(self) -> dict:
        status, _ = self._form("/logout", {})
        self._bot_jar = None
        return {"logged_out": status in (200, 303)}
