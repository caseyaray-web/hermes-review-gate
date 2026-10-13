"""Joined native regression: lost operator retry reply with a synchronous claim."""
import copy
import os

import pytest

from local_first_review import plugin, runtime_escalation, state
from test_review_gate import board
from test_runtime_native import adopt, rm03


def test_joined_operator_retry_lost_unblock_response_reconciles_synchronous_claim_once(rm03, monkeypatch):
    """A real native unblock may claim before its reply is lost, without a resend."""
    b = rm03
    task_id = b.rm_tid
    adopt(b)

    # Drive the original automatic continuation through one actual strong claim
    # and failure, leaving the exact target idle and held for an operator retry.
    plugin.watchdog_tick(board="default")
    original = b.dispatch(expected=task_id)
    assert original.assignee == "strong"
    assert b.kb.block_task(
        b.conn,
        task_id,
        reason="bounded strong run failed",
        kind="needs_input",
        expected_run_id=original.current_run_id,
    )
    b.clear()
    plugin.watchdog_tick(board="default")
    held = state.runtime_escalation_entry(task_id, "default")
    assert held["intent"]["status"] == "held"
    automatic_budget = copy.deepcopy(state.load_state()["recovery_budgets"])
    before = b.show(task_id)
    before_event_ids = {event["id"] for event in before["events"]}

    sends, claimed = [], []
    native_dispatch = plugin._dispatch

    def unblock_then_lose_response(name, args):
        result = native_dispatch(name, args)
        if name == "kanban_unblock":
            sends.append((name, dict(args)))
            tick = b.kbd.dispatch_once(
                b.conn,
                spawn_fn=lambda *_args, **_kwargs: os.getpid(),
                max_spawn=1,
                reconcile_orphans=False,
            )
            assert tick.spawned == [(task_id, "strong", str(b.rm_workspace))]
            current = b.kb.get_task(b.conn, task_id)
            assert current is not None and current.assignee == "strong"
            assert isinstance(current.current_run_id, int)
            claimed.append(current.current_run_id)
            monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(current.current_run_id))
            monkeypatch.setenv("HERMES_PROFILE", "strong")
            delivered = plugin.guard("kanban_show", {})
            assert delivered and "Fresh runtime catch-up coder context" in delivered["message"]
            assert plugin.guard("kanban_show", {}) is None
            raise OSError("lost successful native unblock response")
        return result

    monkeypatch.setattr(plugin, "_dispatch", unblock_then_lose_response)
    retried = runtime_escalation.operator_retry(task_id, "default", "joined-lost-response-1")

    assert retried["ok"], retried
    assert sends == [("kanban_unblock", {"board": "default", "task_id": task_id})]
    assert len(claimed) == 1
    entry = state.runtime_escalation_entry(task_id, "default")
    retry = entry["intent"]["operator_retry_history"][-1]
    assert entry["intent"]["status"] == retry["status"] == "routed"
    assert entry["implementation_run_id"] == retry.get("implementation_run_id", claimed[0]) == claimed[0]
    assert retry["unblock_event_id"] in {event["id"] for event in b.show(task_id)["events"]}
    assert entry["intent"]["transport_history"][-1]["error"] == {
        "type": "OSError",
        "message": "lost successful native unblock response",
    }

    after = b.show(task_id)
    new_events = [event for event in after["events"] if event["id"] not in before_event_ids]
    assert len([event for event in new_events if event["kind"] == "unblocked"]) == 1
    assert len([event for event in new_events if event["kind"] == "claimed" and event["run_id"] == claimed[0]]) == 1
    assert state.load_state()["recovery_budgets"] == automatic_budget
    assert runtime_escalation.admit(task_id, "default", claimed[0], "strong", after) is None
    assert runtime_escalation.admit(task_id, "default", claimed[0] + 1, "strong", after) is not None
    assert runtime_escalation.admit(task_id, "default", claimed[0], "impl", after) is not None

    # Replay the same request id must be state-only: no native transport,
    # no duplicate claim/event, and no budget mutation.
    monkeypatch.setattr(plugin, "_dispatch", lambda *_: pytest.fail("operator retry replay resent native unblock"))
    replay = runtime_escalation.operator_retry(task_id, "default", "joined-lost-response-1")
    assert replay["ok"], replay
    assert b.show(task_id) == after
    assert state.load_state()["recovery_budgets"] == automatic_budget
