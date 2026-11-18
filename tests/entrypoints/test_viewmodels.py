"""View models: what each screen shows and can do, tested without HTTP."""
import pytest

from tests.builders import backlog_ticket, board_over, make_container, payload, sprint, sync, ticket
from wf.entrypoints.web.rendering import render_to_string
from wf.entrypoints.web.viewmodels.base import ViewModel
from wf.entrypoints.web.viewmodels.board import (
    BacklogViewModel, BoardViewModel, ItemViewModel, OutboxViewModel, group_by_stage,
)
from wf.entrypoints.web.viewmodels.links import CheckViewModel, LinksPanelViewModel, LinksViewModel
from wf.entrypoints.web.viewmodels.pipeline import CardViewModel, ColumnViewModel, PipelineViewModel
from wf.entrypoints.web.viewmodels.runs import RunStatusViewModel
from wf.entrypoints.web.viewmodels.ticket import TicketViewModel


@pytest.fixture
def seeded(tmp_path, repo):
    """A container over a board with two sprint tickets (one flagged) and one backlog ticket."""
    container = make_container(tmp_path, repo=repo)
    with board_over(tmp_path / "wf.sqlite") as b:
        b.apply(payload(tickets=[ticket("ABC-1"), ticket("ABC-2", status="Dev in progress")],
                        comments=[{"ticket_key": "ABC-1", "jira_comment_id": "c1", "author": "Ana",
                                   "body": "Keep the old endpoint.", "created": 1}]))
        b.apply(payload(kind="backlog", tickets=[backlog_ticket("ABC-9", assignee_name="Pat Tester")]))
        b.tickets.set_flag("ABC-1", "needs-human", "dev", "scope unclear")
    return container


def test_a_view_model_binds_its_fields_and_itself_to_its_template():
    card = CardViewModel(key="ABC-1", stage="in_dev", summary="s", state={"running": None})
    bound = card.bindings()
    assert bound["t"] is card and bound["vm"] is card and bound["stage"] == "in_dev"
    column = ColumnViewModel(stage="in_dev", label="In development", owner="Dev")
    assert column.bindings()["col"] is column

    class Plain(ViewModel):
        template = "x.html"
        active = "tab"

        def __init__(self):
            self.visible, self._hidden = 1, 2  # a name with a leading underscore is not bound

    plain = Plain()
    assert plain.bindings() == {"visible": 1, "vm": plain, "active": "tab"}


def test_the_board_groups_tickets_in_workflow_order_and_reports_the_sync(seeded):
    vm = BoardViewModel.load(seeded)
    assert [stage for stage, _ in vm.groups] == ["ready_for_dev", "in_dev"] and vm.total == 2
    assert vm.sync["run"]["kind"] == "active" and vm.sync["stale"] is True  # synced at the fixed test time, long ago


def test_group_by_stage_puts_unmapped_tickets_last():
    rows = [{"key": "A", "stage": None}, {"key": "B", "stage": "in_dev"}, {"key": "C", "stage": "backlog"}]
    assert [(s, [r["key"] for r in rs]) for s, rs in group_by_stage(rows)] == [
        ("backlog", ["C"]), ("in_dev", ["B"]), ("(unmapped)", ["A"])]


def test_the_backlog_shows_initials_and_a_stage_label(seeded):
    vm = BacklogViewModel.load(seeded, sent=True)
    assert vm.sent is True and [t["key"] for t in vm.tickets] == ["ABC-9"]
    assert vm.tickets[0]["initials"] == "PT" and vm.tickets[0]["stage_label"] == "To do"
    assert vm.active_sprint["id"] == "101"


def test_an_unknown_ticket_has_no_view_model(seeded):
    assert ItemViewModel.load(seeded, "NOPE-1") is None and TicketViewModel.load(seeded, "NOPE-1") is None
    item = ItemViewModel.load(seeded, "ABC-1")
    assert item.in_active_sprint and item.sprint_name == "Sprint 101" and item.comments[0]["author"] == "Ana"


def test_the_ticket_page_knows_its_flag_and_who_would_run_next(seeded):
    vm = TicketViewModel.load(seeded, "ABC-1", notice="hello")
    assert vm.flag_reason == "scope unclear" and vm.next_role is None and vm.notice == "hello"
    assert TicketViewModel.load(seeded, "ABC-2").next_role == "dev"


def test_answering_a_flag_comments_and_clears_it_but_an_empty_answer_does_nothing(seeded):
    TicketViewModel.answer(seeded, "ABC-1", "   ")
    assert TicketViewModel.load(seeded, "ABC-1").ticket["flag_list"] == ["needs-human"]
    TicketViewModel.answer(seeded, "ABC-1", "Use /login.")
    vm = TicketViewModel.load(seeded, "ABC-1")
    assert vm.ticket["flag_list"] == [] and vm.comments[-1]["body"] == "Use /login."


def test_sending_a_ticket_back_needs_an_allowed_stage_and_a_reason(seeded):
    TicketViewModel.send_back(seeded, "ABC-2", "deployed", "nope")  # not a stage you can send back to
    TicketViewModel.send_back(seeded, "ABC-2", "pr_open", "   ")  # no reason
    assert TicketViewModel.load(seeded, "ABC-2").ticket["stage"] == "in_dev"
    TicketViewModel.send_back(seeded, "ABC-2", "ready_for_qa", "missing a test")  # the user may move anything
    vm = TicketViewModel.load(seeded, "ABC-2")
    assert vm.ticket["stage"] == "ready_for_qa" and vm.comments[-1]["body"] == "Sent back to ready_for_qa: missing a test"


def test_the_pipeline_page_has_a_column_per_stage_with_flagged_cards_explained(seeded):
    vm = PipelineViewModel.load(seeded)
    stages = [c.stage for c in vm.columns]
    assert stages[0] == "backlog" and stages[-1] == "deployed" and len(stages) == 14
    ready = next(c for c in vm.columns if c.stage == "ready_for_dev")
    card = ready.tickets[0]
    assert (card.key, card.flag_list, card.flag_reason, card.role) == ("ABC-1", ["needs-human"], "scope unclear", None)
    assert "scope unclear" in render_to_string(card) and "Clear flag" in render_to_string(card)
    assert vm.queued == 0 and vm.paused is False and vm.access is True


def test_outbox_retry_ignores_an_item_that_cannot_be_retried(seeded):
    OutboxViewModel.retry(seeded, 999)  # no such item: no error
    assert OutboxViewModel.load(seeded).items == []


def test_the_links_panel_reports_a_bad_repo_and_keeps_what_it_has(tmp_path):
    container = make_container(tmp_path)
    vm = LinksPanelViewModel.add_repo(container, "api", "/srv/api")
    assert vm.error is None and [r.name for r in vm.repos] == ["api"]
    again = LinksPanelViewModel.add_repo(container, "BAD NAME", "/x")
    assert "repo name must be" in again.error and [r.name for r in again.repos] == ["api"]
    assert LinksPanelViewModel.add_board(container, "jira", "12", " mine ").boards[0].label == "mine"
    assert "numeric board id" in LinksPanelViewModel.add_board(container, "jira", "abc", "").error
    assert LinksPanelViewModel.remove_repo(container, "api").repos == []
    assert LinksViewModel.load(container).template == "links.html"


def test_checking_links_reports_each_repo_and_board(tmp_path, repo):
    container = make_container(tmp_path, repo=repo)
    container_cfg = container.config()
    container_cfg.add_board("jira", "12")
    container.save_config(container_cfg)

    class Reader:
        def check_board(self, board):
            return type("R", (), {"ok": False, "detail": "no credentials"})()

    rows = CheckViewModel.run(container, Reader()).rows
    assert [(r.kind, r.name, r.ok) for r in rows] == [("repo", "app", True), ("board (jira)", "12", False)]


def test_a_run_that_does_not_exist_has_no_view_model_and_cannot_be_decided(tmp_path):
    container = make_container(tmp_path)
    assert RunStatusViewModel.load(container, "nope") is None
    assert RunStatusViewModel.decide(container, "nope", True) is None
