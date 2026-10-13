"""Joined native proof for runtime retry continuation delivery context."""
import json

from local_first_review import plugin, runtime_escalation, state
from test_runtime_native import adopt

pytest_plugins = ("test_review_gate", "test_runtime_native")


def _continuation_envelope(refusal):
    prefix = "Fresh runtime catch-up coder context: "
    assert refusal and refusal["action"] == "block"
    assert refusal["message"].startswith(prefix)
    return json.loads(refusal["message"][len(prefix):])


def test_joined_operator_retry_delivers_current_authority_and_historical_context(rm03, monkeypatch):
    """Only the freshly claimed retry coder gets one current context envelope."""
    b = rm03
    task_id = b.rm_tid
    adopt(b)
    plugin.watchdog_tick(board="default")
    assert b.dispatch(expected=task_id).assignee == "strong"
    assert b.kb.block_task(b.conn, task_id, reason="bounded strong run failed", kind="needs_input",
                           expected_run_id=b.kb.get_task(b.conn, task_id).current_run_id)
    b.clear()
    plugin.watchdog_tick(board="default")
    held = state.runtime_escalation_entry(task_id, "default")
    assert isinstance(held, dict)
    assert held["intent"]["status"] == "held"

    # The new explicit retry must retain this old delivery as history, then
    # deliver once to the exact fresh native claim.
    with state.locked_state(write=True) as data:
        data["runtime_escalations"][f"default:{task_id}"]["coder_context_delivery_run_id"] = 2274
    (b.rm_workspace / "operator-inspected-partial.py").write_text("preserve this partial retry work\n")

    dispatch = plugin._dispatch
    observed = {}

    def claim_during_unblock(name, args):
        result = dispatch(name, args)
        if name == "kanban_unblock":
            task = b.dispatch(expected=task_id)
            run_id = task.current_run_id
            assert task.assignee == "strong" and isinstance(run_id, int)
            observed["run_id"] = run_id

            envelope = _continuation_envelope(plugin.guard("kanban_show", {}))
            observed["envelope"] = envelope
            # One-use delivery permits the same run's ordinary tools afterward.
            assert plugin.guard("kanban_show", {}) is None

            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 1))
            wrong_run = plugin.guard("kanban_show", {})
            assert wrong_run and wrong_run["action"] == "block"
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
            monkeypatch.setenv("HERMES_PROFILE", "impl")
            wrong_profile = plugin.guard("kanban_show", {})
            assert wrong_profile and wrong_profile["action"] == "block"
            monkeypatch.setenv("HERMES_PROFILE", "strong")
        return result

    monkeypatch.setattr(plugin, "_dispatch", claim_during_unblock)
    b.clear()
    result = runtime_escalation.operator_retry(task_id, "default", "continuation-context-retry")
    assert result["ok"], result

    entry = state.runtime_escalation_entry(task_id, "default")
    assert isinstance(entry, dict)
    retry = entry["intent"]["operator_retry_history"][-1]
    envelope = observed["envelope"]
    assert envelope["context"] == entry["coder_context"]
    assert envelope["admission"] == {
        "run_id": observed["run_id"],
        "profile": "strong",
        "workspace_path": str(b.rm_workspace),
        "route": {
            "board": "default", "task_id": task_id,
            "implementation_profile": "strong", "reviewer_profile": "post-review",
        },
    }
    evidence = envelope["historical_retry_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["request_id"] == retry["request_id"]
    assert evidence[0]["prior_coder_context_delivery_run_id"] == 2274
    assert evidence[0]["prior_checkpoint"] == held["checkpoint"]
    assert evidence[0]["checkpoint"] == entry["checkpoint"]
    assert evidence[0]["status"] == "unblock_attempted"
    assert envelope["next_action"] == "Inspect and continue the existing dirty checkpoint; do not accept or clean it."
    assert envelope["history_authority"] == (
        "Historical retry evidence is context only, not current blocked-task authority; "
        "only the current routed admission above authorizes tools."
    )
    assert entry["coder_context_delivery_run_id"] == observed["run_id"]
