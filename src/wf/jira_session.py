"""Jira work done by a Claude Code session, started from the web UI.

The Atlassian connector lives in the user's Claude Code login, not in this app. So the
UI starts `claude -p` with that connector, asks it for one JSON answer, and this module
checks and applies the answer. The session may only use Jira's read and transition
tools, and built-in tools are off. Every write still goes through wf.outbox, which
checks Jira's current status first.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from wf.model import ProviderNotAllowed
from wf.model.claude_code import CLAUDE_BIN, subscription_env

READ_TOOLS = [
    "mcp__atlassian__getAccessibleAtlassianResources",
    "mcp__atlassian__searchJiraIssuesUsingJql",
    "mcp__atlassian__getJiraIssue",
    "mcp__atlassian__getTransitionsForJiraIssue",
]
WRITE_TOOLS = ["mcp__atlassian__transitionJiraIssue", "mcp__atlassian__editJiraIssue"]
BUILTIN_OFF = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch"]

SYNC_JQL = ("assignee = currentUser() AND (sprint in openSprints() "
            "OR (sprint is EMPTY AND statusCategory != Done))")

SYNC_PROMPT = f"""You are the Jira reader for the wf workflow. Use only the Atlassian tools.

1. Call getAccessibleAtlassianResources and take the first site's id as cloudId.
2. Call searchJiraIssuesUsingJql with jql: {SYNC_JQL}
   Ask for these fields: summary, description, status, issuetype, priority, assignee, labels, project, updated, customfield_10020, issuelinks, comment.
   Page through all results.
3. For each distinct project key, record the statuses seen on its tickets. For each ticket, call getTransitionsForJiraIssue and add each transition's target status too.

Reply with ONLY one JSON object, no prose, in this shape:
{{"kind": "active",
  "projects": [{{"key": "...", "name": "...", "statuses": [{{"name": "...", "category": "new|indeterminate|done", "source": "observed|workflow"}}]}}],
  "sprints": [{{"id": "...", "board_id": "...", "name": "...", "state": "active", "goal": null, "start_date": unix_seconds_or_null, "end_date": unix_seconds_or_null, "complete_date": null}}],
  "tickets": [{{"key": "...", "project_key": "...", "jira_id": "...", "summary": "...", "description": "...", "acceptance": "", "issue_type": "...", "priority": "...", "points": null, "assignee_id": "...", "status": "...", "status_category": "...", "labels": [], "sprint_id": "...", "updated": unix_seconds, "assignee_name": "...", "links": [{{"type": "...", "direction": "inward|outward", "key": "...", "summary": "...", "status": "..."}}]}}],
  "comments": [{{"ticket_key": "...", "jira_comment_id": "...", "author": "...", "body": "...", "created": unix_seconds, "updated": unix_seconds_or_null}}]}}
Use the sprint's own id from customfield_10020. Dates are Unix seconds."""


def _observe_prompt(keys: list[str]) -> str:
    return f"""You are the Jira reader for the wf workflow. Use only the Atlassian tools.

For each ticket key below, call getJiraIssue (fields: status) and getTransitionsForJiraIssue.

Tickets: {json.dumps(keys)}

Reply with ONLY one JSON object, no prose:
{{"observations": {{"KEY-1": {{"status": "current status name", "transitions": [{{"id": "transition id", "to": "target status name"}}]}}}}}}
Leave out any ticket you could not read."""


def _execute_prompt(sends: list[dict]) -> str:
    return f"""You are the Jira writer for the wf workflow. Use only the Atlassian tools.

Apply exactly these transitions, one call each, with transitionJiraIssue. Do not do anything else:
{json.dumps(sends, indent=2)}

For each one, call transitionJiraIssue with issueIdOrKey = key and transition = {{"id": transition_id}}.

Reply with ONLY one JSON array, no prose, with one entry per item:
[{{"id": <the item's id>, "ok": true|false, "jira_status": "<status after the call>", "error": "<text if it failed>"}}]"""


Runner = Callable[[list[str], Path, float], "subprocess.CompletedProcess[str]"]


def default_runner(argv: list[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
                          env=subscription_env())


def _json_from(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise
        return json.loads(text[start:])


def session(prompt: str, *, allowed: list[str], access: bool, workdir: Path, model: str,
            claude_bin: str | None, runner: Runner | None = None, timeout: float = 900.0) -> Any:
    """Run one Claude Code session with the Atlassian connector and return its JSON answer."""
    if not access:
        raise ProviderNotAllowed("Claude Code runs are not enabled; set claude_code_access in the model config")
    if not claude_bin:
        raise ProviderNotAllowed("the claude CLI is not on PATH")
    workdir.mkdir(parents=True, exist_ok=True)
    argv = [claude_bin, "-p", prompt, "--model", model, "--output-format", "json",
            "--allowedTools", *allowed, "--disallowedTools", *BUILTIN_OFF,
            "--permission-mode", "default", "--no-session-persistence"]
    proc = (runner or default_runner)(argv, workdir, timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code session failed (exit {proc.returncode}): {(proc.stderr or '')[-400:]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"Claude Code session reported an error: {str(envelope.get('result'))[:300]}")
    return _json_from(envelope.get("result", ""))


def fetch_sync_payload(**kw) -> dict:
    payload = session(SYNC_PROMPT, allowed=READ_TOOLS, **kw)
    if not isinstance(payload, dict) or payload.get("kind") != "active":
        raise RuntimeError("the session did not return an active sync payload")
    return payload


def move_to_sprint(key: str, sprint_id: str, **kw) -> None:
    """Put one of the user's tickets into a sprint in Jira. Nothing else on the ticket changes."""
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key) or not str(sprint_id).isdigit():
        raise ValueError("bad ticket key or sprint id")
    out = session(
        "You are the Jira writer for the wf workflow. Use only the Atlassian tools.\n\n"
        "1. Call getAccessibleAtlassianResources and take the first site's id as cloudId.\n"
        f"2. Call editJiraIssue for issue {key} with fields {{\"customfield_10020\": {int(sprint_id)}}}. Change nothing else.\n\n"
        'Reply with ONLY one JSON object: {"ok": true} if Jira accepted it, otherwise {"ok": false, "error": "..."}.',
        allowed=READ_TOOLS + ["mcp__atlassian__editJiraIssue"], **kw)
    if not isinstance(out, dict) or not out.get("ok"):
        raise RuntimeError(f"Jira did not move {key} to the sprint: {out}")


def fetch_observations(keys: list[str], **kw) -> dict:
    if not keys:
        return {}
    out = session(_observe_prompt(keys), allowed=READ_TOOLS, **kw)
    return out.get("observations", {}) if isinstance(out, dict) else {}


def execute_sends(sends: list[dict], **kw) -> list[dict]:
    if not sends:
        return []
    out = session(_execute_prompt(sends), allowed=READ_TOOLS + WRITE_TOOLS, **kw)
    return out if isinstance(out, list) else []
