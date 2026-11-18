"""The Jira trigger path (intake, supervisor, pipeline) with in-memory logs: no database."""
from tests.fakes import MemoryChangeLog, MemoryLifecycleLog
from wf.application.tracker.intake import Intake
from wf.application.tracker.supervisor import Supervisor
from wf.application.tracker.triggers import TriggerPipeline

MAPPING = {"Ready for dev": "ready_for_dev", "QA Approved": "qa_approved", "Done": "deployed"}


def body(*changes, labels=()):
    return {"changelog": {"histories": [{"id": hid, "created": "t", "items": [
        {"field": "status", "fromString": a, "toString": b}]} for hid, a, b in changes]},
        "fields": {"labels": list(labels)}}


def build(bodies, dispatched=None, max_dispatches=10, now=100.0):
    changes, log = MemoryChangeLog(), MemoryLifecycleLog()
    sent = [] if dispatched is None else dispatched
    sup = Supervisor(log, lambda: MAPPING, lambda owner, key, status: sent.append((owner, key, status)) or True,
                     max_dispatches)
    intake = Intake(changes, lambda key: bodies[key], lambda: now)
    pipe = TriggerPipeline(changes=changes, intake=intake, supervisor=sup, search_keys=lambda: list(bodies),
                           clock=lambda: now)
    return pipe, changes, log, sent


def test_a_webhook_records_each_change_once_and_dispatches_to_the_owner():
    pipe, changes, log, sent = build({"K-1": body(("1", "To do", "Ready for dev"))})
    assert [d.action for d in pipe.on_webhook("K-1")] == ["dispatch"]
    assert pipe.on_webhook("K-1") == []  # the same change is not routed twice
    assert sent == [("dev", "K-1", "Ready for dev")] and changes.last_webhook_at() is not None
    assert log.history("K-1")[0]["dispatched"] == 1


def test_labels_decide_the_owner_and_an_unmapped_status_is_reported_not_guessed():
    pipe, _, _, sent = build({"K-1": body(("1", "a", "Ready for dev"), ("2", "b", "Nowhere"), labels=["hotfix"])})
    actions = [(d.action, d.owner) for d in pipe.on_webhook("K-1")]
    assert actions == [("dispatch", "oncall"), ("unknown", None)]
    assert sent == [("oncall", "K-1", "Ready for dev")]


def test_the_kill_switch_halts_everything_and_the_loop_guard_escalates():
    pipe, _, log, sent = build({"K-1": body(("1", "a", "Ready for dev"))})
    log.set_kill(True)
    assert pipe.on_webhook("K-1")[0].action == "halt" and sent == []

    pipe, _, log, sent = build({"K-1": body(*[(str(i), "a", "Ready for dev") for i in range(4)])}, max_dispatches=2)
    assert [d.action for d in pipe.on_webhook("K-1")] == ["dispatch", "dispatch", "escalate", "escalate"]
    assert len(sent) == 2


def test_the_poll_waits_while_webhooks_arrive_and_runs_when_they_stop():
    pipe, changes, _, sent = build({"K-1": body(("1", "a", "Ready for dev"))}, now=10_000.0)
    changes.mark_webhook(9_000.0)  # a webhook arrived recently
    assert pipe.poll() == [] and sent == []
    changes.mark_webhook(1_000.0)  # more than two hours ago
    assert [d.action for d in pipe.poll()] == ["dispatch"]
