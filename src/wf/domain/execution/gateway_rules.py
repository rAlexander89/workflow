"""The rules the agent gateway enforces: what a run may say, fetch, and sign in with."""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit

# Requests to a remote app carry this header. An edge firewall can use it to let the runner through
# its browser checks when the request carries the matching secret. The firewall is configured outside this tool.
RUNNER_SECRET_HEADER = "X-Wf-Runner-Secret"

# fetch_local returns this many bytes of a body. Big enough for a stylesheet or a page, so QA can read
# the whole thing (a media query near the end of a CSS file used to be cut off at 4,000 bytes).
MAX_FETCH_BYTES = 512 * 1024

# Sign-up pages create accounts. QA never needs them, so fetch_local refuses them.
SIGNUP_SEGMENTS = ("register", "signup", "sign-up", "sign_up")

MAX_COMMENT = 600


def runner_headers(url: str, role: str, secret: str, secret_host: str) -> dict[str, str]:
    """Headers for a request the runner makes to the app.

    The User-Agent names the bot, so it can be told apart from a browser and from Python's default.
    The secret goes only to secret_host (the dev deploy). Any other host, including prod and the
    local app, never sees it.
    """
    headers = {"User-Agent": f"wf-{role or 'agent'}-bot/1.0"}
    host = (urlsplit(url).hostname or "").lower()
    if secret and secret_host and host == secret_host.lower():
        headers[RUNNER_SECRET_HEADER] = secret
    return headers


def is_signup_path(path: str) -> bool:
    """True when a path points at a sign-up page.

    Decodes percent-escapes and resolves `..` first, so /%72egister and /login/../register
    are caught as well as /register/ and /register?x=1.
    """
    clean = unquote(path.split("?", 1)[0].split("#", 1)[0])
    normal = posixpath.normpath("/" + clean.lstrip("/"))
    first = normal.strip("/").split("/", 1)[0].lower()
    return first in SIGNUP_SEGMENTS


def check_comment_length(body: str) -> None:
    """A comment is short: the decision or blocker first, then one line per open question."""
    body = body.strip()
    if len(body) > MAX_COMMENT:
        raise ValueError(f"comment is {len(body)} characters; keep it under {MAX_COMMENT}. "
                         "Lead with the decision or blocker, then one line per open question.")


def bot_password(credentials_text: str, role: str) -> str:
    """The role's bot password from the credentials file's text (`<role>_password=...` lines)."""
    want = f"{role}_password"
    for line in credentials_text.splitlines():
        if "=" in line and line.split("=", 1)[0].strip().lower() == want:
            return line.split("=", 1)[1]
    raise ValueError(f"no password for the {role} bot in the credentials file")
