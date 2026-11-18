"""Local environments: start and stop a project's app so the agents (and you) can use it.

The command comes from the config (agents.app_cmd). "{port}" is replaced with a free port.
The running process is recorded in a state file under the runs folder, so `down` can stop it
later, even from a new terminal.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def _state_file(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.json"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(state_dir: Path, name: str) -> dict | None:
    f = _state_file(state_dir, name)
    if not f.exists():
        return None
    st = json.loads(f.read_text())
    if st.get("external"):
        st["running"] = answers(st["url"])
    else:
        st["running"] = _alive(st["pid"])
    return st


def answers(url: str, timeout: float = 3.0) -> bool:
    try:
        urllib.request.urlopen(url.rstrip("/") + "/", timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True  # it answered, even if the page is an error
    except Exception:
        return False


def up(name: str, *, repo: Path, app_cmd: str, state_dir: Path, timeout: float = 60.0,
       app_url: str = "") -> dict:
    """Use the app already running at app_url if it answers; otherwise start app_cmd."""
    if app_url and answers(app_url):
        st = {"name": name, "external": True, "pid": None, "port": None, "url": app_url.rstrip("/"),
              "repo": str(repo), "started_at": int(time.time())}
        state_dir.mkdir(parents=True, exist_ok=True)
        _state_file(state_dir, name).write_text(json.dumps(st, indent=2) + "\n")
        st["running"] = True
        return st
    if not app_cmd:
        raise ValueError("no app is running at agents.app_url and no app command is set (agents.app_cmd)")
    current = status(state_dir, name)
    if current and current["running"]:
        return current
    port = _free_port()
    proc = subprocess.Popen(shlex.split(app_cmd.replace("{port}", str(port))), cwd=repo,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the app exited during startup (code {proc.returncode})")
        try:
            urllib.request.urlopen(url + "/", timeout=2).close()
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError(f"the app did not answer on {url} within {int(timeout)}s")
    state_dir.mkdir(parents=True, exist_ok=True)
    st = {"name": name, "pid": proc.pid, "port": port, "url": url, "repo": str(repo), "started_at": int(time.time())}
    _state_file(state_dir, name).write_text(json.dumps(st, indent=2) + "\n")
    st["running"] = True
    return st


def down(name: str, *, state_dir: Path, timeout: float = 10.0) -> bool:
    st = status(state_dir, name)
    if st is None:
        return False
    if st.get("external"):
        # Not ours to stop: forget the link, leave the running app alone.
        _state_file(state_dir, name).unlink(missing_ok=True)
        return True
    if st["running"]:
        try:
            os.killpg(st["pid"], signal.SIGTERM)
        except OSError:
            os.kill(st["pid"], signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline and _alive(st["pid"]):
            time.sleep(0.2)
        if _alive(st["pid"]):
            try:
                os.killpg(st["pid"], signal.SIGKILL)
            except OSError:
                pass
    _state_file(state_dir, name).unlink(missing_ok=True)
    return True


class LocalAppEnvironment:
    """AppEnvironment over a local process recorded in a state file."""

    def status(self, state_dir: Path, name: str) -> dict | None:
        return status(state_dir, name)

    def up(self, name: str, *, repo: Path, app_cmd: str, state_dir: Path, app_url: str = "") -> dict:
        return up(name, repo=repo, app_cmd=app_cmd, state_dir=state_dir, app_url=app_url)

    def down(self, name: str, *, state_dir: Path) -> bool:
        return down(name, state_dir=state_dir)
