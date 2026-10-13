"""Regression proof for repeat operator holds under native auto-decompose."""

from hermes_cli import kanban_decompose

from local_first_review import state
from test_review_gate import board


def test_repeated_operator_hold_uses_scheduled_lane_not_triage(board):
    """The native recurrence boundary must not feed the auto-decomposer."""
    b = board
    state.activate_board("default", activation_id="stable-hold")
    state.set_escalation_policy(
        "default", enabled=False, normal_correction_limit=1, max_attempts=1,
    )

    assert b.dispatch().assignee == "impl"
    assert b.call("finish_implementation", summary="Initial candidate.")["ok"]
    assert b.dispatch().assignee == "review"
    assert b.call("submit_review", verdict="changes_requested", rationale="First correction.")["ok"]

    assert b.dispatch().assignee == "impl"
    (b.repo / "implementation.txt").write_text("first correction\n")
    b.git("add", ".")
    b.git("commit", "-qm", "first correction")
    assert b.call("finish_implementation", summary="First corrected candidate.")["ok"]
    assert b.dispatch().assignee == "review"
    assert b.call("submit_review", verdict="changes_requested", rationale="Still needs operator review.")["ok"]
    first_hold = b.show()["task"]
    assert (first_hold["status"], first_hold["block_kind"], first_hold["block_recurrences"]) == (
        "scheduled", "needs_input", 1,
    )
    # The fixture enrollment itself was an explicit needs_input hold.  Native
    # retains that recurrence through its initial unblock, so this is already
    # the recurrence that block_task would route to triage.  Scheduling must
    # preserve that native hold evidence instead of dispatching/decomposing it.
    assert kanban_decompose.list_triage_ids() == []
    assert b.kb.recompute_ready(b.conn) == 0
    b.clear()
    first_tick = b.kbd.dispatch_once(
        b.conn,
        spawn_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stable hold redispatched")),
        max_spawn=1,
        reconcile_orphans=False,
    )
    assert not first_tick.spawned
    assert b.kb.get_task(b.conn, b.child).status == "todo"
