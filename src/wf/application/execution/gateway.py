"""What an agent run may do: the tools its gateway offers, with the role and ticket fixed by its scope.

An agent can't change its role or ticket per call. Board changes go through the ticket commands, the
worktree through the Toolbox, and the app through the AppSession.
"""

from __future__ import annotations

from wf.application.board.tickets import TicketCommands
from wf.application.ports.board import BoardQueries
from wf.application.ports.execution import AppSession, Toolbox
from wf.domain.board.errors import BoardError
from wf.domain.execution import gateway_rules


class RunTools:
    def __init__(self, *, role: str, key: str, commands: TicketCommands, queries: BoardQueries,
                 toolbox: Toolbox, app: AppSession):
        self.role = role
        self.key = key
        self._commands = commands
        self._queries = queries
        self._toolbox = toolbox
        self._app = app

    # --- the board --------------------------------------------------------------------------

    def get_ticket(self) -> dict:
        detail = self._queries.ticket_detail(self.key)
        if detail is None:
            raise BoardError(f"no ticket {self.key}")
        return detail

    def move_ticket(self, stage: str, reason: str) -> dict:
        self._commands.move_stage(self.key, stage, self.role, reason)
        return {"moved_to": stage}

    def link_blocking(self, prerequisite: str, reason: str) -> dict:
        self._commands.add_block(prerequisite, self.key, self.role, reason)
        return {"linked": prerequisite}

    def set_order(self, position: int) -> dict:
        self._commands.set_order(self.key, int(position), self.role)
        return {"position": int(position)}

    def flag_ticket(self, flag: str, reason: str) -> dict:
        self._commands.set_flag(self.key, flag, self.role, reason)
        return {"flagged": flag}

    def post_comment(self, body: str) -> dict:
        gateway_rules.check_comment_length(body)
        return {"comment_id": self._commands.add_comment(self.key, self.role, body)}

    def request_perf_check(self, reason: str) -> dict:
        self._commands.request_perf_check(self.key, self.role, reason)
        return {"requested": True}

    def approve_ticket(self) -> dict:
        head = self._toolbox.head()
        if self.role == "pr_review":
            ok, stray = self._toolbox.stray_commits()
            if not ok:
                raise PermissionError(
                    f"not approving: the branch would also add commits that are not {self.key}, "
                    f"starting with {stray[0][:70]!r}. Use request_changes and say which commits to remove.")
        self._commands.record_approval(self.key, self.role, head)
        stage = self._commands.get(self.key).stage
        if self.role == "pr_review" and stage in ("pr_open", "pr_in_review"):
            self._commands.move_stage(self.key, "ready_for_perf_review", self.role,
                                      f"PR review approved at {head[:8]}")
        if self.role == "perf_review" and stage == "ready_for_perf_review":
            self._commands.move_stage(self.key, "ready_for_qa", self.role,
                                      f"code performance review approved at {head[:8]}")
        return {"approved": head}

    def request_changes(self, reason: str) -> dict:
        self._commands.move_stage(self.key, "in_dev", self.role, reason)
        self._commands.add_comment(self.key, self.role, reason)
        return {"moved_to": "in_dev"}

    # --- the worktree ----------------------------------------------------------------------

    def run_tests(self) -> dict:
        return self._toolbox.run_tests()

    def commit(self, message: str) -> dict:
        return {"commit": self._toolbox.commit(message)}

    # --- the running app --------------------------------------------------------------------

    def start_app(self) -> dict:
        return self._app.start_app()

    def fetch_local(self, path: str) -> dict:
        path = path if path.startswith("/") else "/" + path
        if gateway_rules.is_signup_path(path):
            raise ValueError("sign-up pages are not part of QA; the bot only signs in with bot_login")
        return self._app.fetch_local(path)

    def stop_app(self) -> dict:
        return self._app.stop_app()

    def bot_login(self) -> dict:
        return self._app.bot_login()

    def bot_logout(self) -> dict:
        return self._app.bot_logout()
