import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from local_first_review import plugin, state


def test_native_review_claim_has_no_authorizing_claim_hook_before_pretool():
    """Native-child fencing forbids mutation here; inspect the exact core branch."""
    import importlib.util

    spec = importlib.util.find_spec("hermes_cli")
    assert spec and spec.submodule_search_locations
    source = (Path(next(iter(spec.submodule_search_locations))) / "kanban_db.py").read_text()
    start = source.index("def claim_review_task(")
    end = source.index("\ndef _retry_status_for_run(", start)
    assert "_fire_task_hook" not in source[start:end]


def _binding():
    return {"board": "default", "task_id": "task", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/work", "policy_activation_id": "a", "policy_native_run_watermark": 0, "native_run_id": 1}


def test_checkpoint_hashes_untracked_names_not_contents(tmp_path):
    (tmp_path / ".git").mkdir(); (tmp_path / "secret.txt").write_text("very secret content")
    checkpoint = plugin._workspace_checkpoint(str(tmp_path), head="a" * 40, porcelain="?? secret.txt\n")
    assert checkpoint["dirty"] == [{"path": "secret.txt", "sha256": hashlib.sha256(b"very secret content").hexdigest()}]
    assert "very secret content" not in repr(checkpoint)


def test_recovery_admission_accepts_native_ready_claim_omission_and_pins_first_normal_tool(monkeypatch):
    entry = {"failed_run_id": 1, "phase": "implementation", "workspace_path": "/work", "expected_source_status": "ready", "checkpoint": {"head": "a" * 40, "dirty": []}, "intent": {"status": "unblock_verified", "native_status": "ready"}, "authorized_run_id": 2, "authorized_profile": "impl"}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task"); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "2"); monkeypatch.setenv("HERMES_KANBAN_BOARD", "default"); monkeypatch.setenv("HERMES_PROFILE", "impl")
    monkeypatch.setattr(plugin, "task_binding", lambda *_: _binding()); monkeypatch.setattr(plugin, "worker_profile", lambda: "impl")
    monkeypatch.setattr(plugin, "profile_exists", lambda _: True); monkeypatch.setattr(plugin, "recovery_entry", lambda *_, **__: entry)
    monkeypatch.setattr(plugin, "_show", lambda _: {"task": {"id": "task", "status": "running", "assignee": "impl", "workspace_path": "/work", "current_run_id": 2}, "runs": [{"id": 2, "profile": "impl", "status": "running", "ended_at": None}], "events": [{"kind": "claimed", "run_id": 2, "payload": {"lock": "native"}}]})
    monkeypatch.setattr(plugin, "_workspace_checkpoint", lambda *_: entry["checkpoint"])
    pinned = []; monkeypatch.setattr(plugin, "pin_recovery_receipt", lambda *args: pinned.append(args))
    monkeypatch.setattr(plugin, "authorize_recovery_run", lambda *_: entry.update({"authorized_run_id": 2, "authorized_profile": "impl"}))
    assert plugin.guard("terminal", {}) is None
    assert pinned == [("default", "task", 1, "implementation", 2, {"tool": "terminal", "profile": "impl"})]
    entry["receipt"] = {"run_id": 2}
    assert plugin.guard("terminal", {}) is None


def test_recovery_read_error_fails_closed(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: (_ for _ in ()).throw(OSError("locked")))
    result = plugin.guard("terminal", {})
    assert result and result["action"] == "block" and "unreadable" in result["message"]


def test_effective_escalation_route_requires_exact_current_run_and_profile(monkeypatch):
    """A watchdog-published route is not authority for an arbitrary process."""
    binding = _binding()
    entry = {"binding": binding, "current_attempt": 1, "attempts": [{"attempt": 1, "intent": {"status": "routed"}}]}
    show = {"task": {"id": "task", "status": "running", "assignee": "strong", "current_run_id": 22},
            "runs": [{"id": 22, "profile": "strong", "status": "running", "ended_at": None}],
            "events": [{"kind": "claimed", "run_id": 22, "payload": {"source_status": "ready"}}]}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "23")
    monkeypatch.setenv("HERMES_PROFILE", "strong")
    monkeypatch.setattr(plugin, "escalation_entry", lambda *_: entry)
    monkeypatch.setattr(plugin, "task_binding", lambda *_: binding)
    monkeypatch.setattr(plugin, "trusted_routing", lambda *_: {"implementation_profile": "strong", "reviewer_profile": "post-review"})
    monkeypatch.setattr(plugin, "_show", lambda *_: show)
    monkeypatch.setattr(plugin, "worker_profile", lambda: "strong")
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: None)

    refused = plugin.guard("terminal", {})
    assert refused and refused["action"] == "block" and "Effective escalation route" in refused["message"]

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "22")
    assert plugin.guard("terminal", {}) is None
    monkeypatch.setattr(plugin, "worker_profile", lambda: "other")
    refused = plugin.guard("terminal", {})
    assert refused and refused["action"] == "block" and "Effective escalation route" in refused["message"]


def test_runtime_coder_context_is_not_delivered_to_the_post_escalation_reviewer(monkeypatch):
    binding = _binding()
    runtime = {"binding": binding, "intent": {"status": "routed"},
               "attempts": [{"implementation_profile": "terra", "reviewer_profile": "terra-review"}],
               "coder_context": {"checkpoint": {}}}
    delivered = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "22")
    monkeypatch.setenv("HERMES_PROFILE", "terra-review")
    monkeypatch.setattr(plugin, "runtime_escalation_entry", lambda *_: runtime)
    monkeypatch.setattr(plugin, "_routed_escalation_guard", lambda *_: None)
    monkeypatch.setattr(plugin, "task_binding", lambda *_: binding)
    monkeypatch.setattr(plugin, "trusted_routing", lambda *_: {"implementation_profile": "terra", "reviewer_profile": "terra-review"})
    monkeypatch.setattr(plugin, "worker_profile", lambda: "terra-review")
    monkeypatch.setattr(plugin, "deliver_runtime_coder_context", lambda *args: delivered.append(args) or {"checkpoint": {}})
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: None)

    assert plugin.guard("terminal", {}) is None
    assert delivered == []


def test_reconciled_escalation_still_refuses_direct_lifecycle_bypass(monkeypatch):
    """First-tool reconciliation must flow into the normal lifecycle fence."""
    binding = _binding()
    entry = {"binding": binding, "current_attempt": 1, "attempts": [{"attempt": 1, "intent": {"status": "changes_requested_pending", "review_run_id": 9}}]}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "22")
    monkeypatch.setenv("HERMES_PROFILE", "strong")
    routed = {"binding": binding, "current_attempt": 1, "attempts": [{"attempt": 1, "intent": {"status": "routed"}}]}
    reads = [entry, routed]
    monkeypatch.setattr(plugin, "escalation_entry", lambda *_: reads.pop(0) if reads else routed)
    monkeypatch.setattr(plugin, "_reconcile_pending_escalation", lambda *_, **__: "routed")
    monkeypatch.setattr(plugin, "_routed_escalation_guard", lambda *_: None)
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: None)
    monkeypatch.setattr(plugin, "is_managed", lambda *_: True)
    monkeypatch.setattr(plugin, "_show", lambda *_: {"task": {"id": "task"}, "runs": []})
    monkeypatch.setattr(plugin, "_binding", lambda *_: ("task", binding))

    refused = plugin.guard("kanban_complete", {"summary": "bypass"})
    assert refused and refused["action"] == "block"
    assert "finish_implementation" in refused["message"]


def test_exact_unbound_rm02_requires_one_postwatermark_gave_up_with_max_one():
    policy = {"native_run_watermark": 4, "implementation_profile": "impl"}
    show = {"task": {"status": "blocked"}, "runs": [{"id": 5, "profile": "impl", "outcome": "gave_up"}], "events": [
        # Native ready claims omit source_status.  The terminal event carries
        # retry_status resolved from that claim by the native dispatcher.
        {"kind": "claimed", "run_id": 5, "payload": {"lock": "native", "expires": 1}},
        {"kind": "spawned", "run_id": 5, "payload": {"pid": 123}},
        {"kind": "gave_up", "run_id": 5, "payload": {"trigger_outcome": "timed_out", "effective_limit": 1, "retry_status": "ready", "budget_used": 180, "budget_max": 180, "error": "Iteration budget exhausted (180/180) — task could not complete within the allowed iterations"}},
    ]}
    assert plugin._first_unbound_gave_up(policy, show) == (show["runs"][0], "implementation")
    show["events"][2]["payload"]["effective_limit"] = 2
    assert plugin._first_unbound_gave_up(policy, show) is None


def test_missing_claim_source_status_requires_exact_terminal_retry_evidence():
    failed = {"id": 5, "outcome": "gave_up"}
    show = {"events": [
        {"kind": "claimed", "run_id": 5, "payload": {"lock": "native"}},
        {"kind": "gave_up", "run_id": 4, "payload": {"retry_status": "ready"}},
    ]}
    assert plugin._failed_phase(show, failed) is None
    show["events"][1]["run_id"] = 5
    show["events"][1]["payload"]["retry_status"] = "review"
    assert plugin._failed_phase(show, failed) == "review"


def test_recovery_accepts_only_started_worker_iteration_exhaustion():
    failed = {"id": 5, "outcome": "gave_up"}
    show = {"events": [
        {"kind": "spawned", "run_id": 5, "payload": {"pid": 123}},
        {"kind": "gave_up", "run_id": 5, "payload": {
            "trigger_outcome": "timed_out", "effective_limit": 1, "retry_status": "review",
            "budget_used": 180, "budget_max": 180,
            "error": "Iteration budget exhausted (180/180) — task could not complete within the allowed iterations",
        }},
    ]}
    assert plugin._allowed_terminal_failure(show, failed)
    # Startup/spawn failure is not a surrogate for a worker which actually ran.
    show["events"][1]["payload"]["trigger_outcome"] = "spawn_failed"
    assert not plugin._allowed_terminal_failure(show, failed)
    # A manually-created needs_input hold has no same-run native terminal
    # dispatcher event and therefore cannot be recovered as a retry.
    assert not plugin._allowed_terminal_failure({"events": [{"kind": "blocked", "run_id": 5,
        "payload": {"kind": "needs_input"}}]}, failed)


def test_escalation_reconciliation_scope_skips_reentrant_dispatch_tick(tmp_path):
    """A synchronous native hook cannot take a second flock and deadlock."""
    state_file = tmp_path / "local-first-review.json"
    # Run the exact same-thread recursion in a child so a regression is bounded
    # by a process timeout instead of wedging the parent test session.
    code = r'''
from pathlib import Path
import sys
from local_first_review import plugin, state

state.state_path = lambda: Path(sys.argv[1])
binding = {"board": "default", "task_id": "task", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/work"}
data = state._empty_state()
data["tasks"]["default:task"] = binding
data["boards"]["default"] = {
    "activation_id": "a", "native_run_watermark": 0,
    "implementation_profile": "impl", "reviewer_profile": "review",
    "recovery": {"enabled": False, "max_per_phase": 1},
    "escalation": {"enabled": True, "normal_correction_limit": 1, "max_attempts": 1,
                   "implementation_profile": "strong", "reviewer_profile": "post-review"},
}
data["escalations"]["default:task"] = {
    "board": "default", "task_id": "task", "binding": binding,
    "consumed_attempts": 1, "current_attempt": 1,
    "attempts": [{"attempt": 1, "implementation_profile": "strong", "reviewer_profile": "post-review",
                  "origin": binding, "intent": {"status": "changes_requested_pending", "review_run_id": 7,
                  "candidate": {}, "change_count": 1}}],
}
state.save_state(data)
with state.locked_state(write=True):
    with plugin._escalation_reconciliation_scope("default", "task"):
        plugin.watchdog_tick(board="default")
print("reentrant tick fenced")
'''
    result = subprocess.run([sys.executable, "-c", code, str(state_file)], cwd=Path(__file__).parents[1],
                            text=True, capture_output=True, timeout=3, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reentrant tick fenced"


def test_claim_hook_does_not_authorize_without_exact_native_evidence(monkeypatch):
    """A malformed/wrong-phase hook cannot poison a later legitimate admission."""
    recorded = []
    entry = {"failed_run_id": 1, "phase": "implementation", "intent": {"status": "unblock_verified"}}
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: entry)
    monkeypatch.setattr(plugin, "authorize_recovery_run", lambda *args: recorded.append(args))
    plugin.watchdog_claimed(task_id="task", board="default", assignee="impl", run_id=2, profile_name="default")
    assert recorded == []


def test_terminal_before_admission_requires_one_exact_ended_replacement(monkeypatch):
    entry = {"board": "default", "task_id": "task", "failed_run_id": 4, "phase": "implementation",
             "expected_source_status": "ready"}
    monkeypatch.setattr(plugin, "task_binding", lambda *_: _binding())
    show = {"task": {"status": "blocked", "current_run_id": 5, "assignee": "impl"}, "runs": [
        {"id": 4, "profile": "impl", "status": "blocked", "ended_at": 1},
        {"id": 5, "profile": "impl", "status": "blocked", "ended_at": 2},
    ], "events": [{"kind": "claimed", "run_id": 5, "payload": {}}]}
    assert plugin._terminal_unadmitted_replacement(show, entry)

    # The original failure is still blocked after reservation: no successor is
    # never permission to release the lease.
    assert not plugin._terminal_unadmitted_replacement({**show, "runs": show["runs"][:1],
        "task": {"status": "blocked", "current_run_id": 4, "assignee": "impl"}}, entry)
    # A wrong-profile successor or another live run is unrelated/ambiguous.
    show["runs"][1]["profile"] = "other"
    assert not plugin._terminal_unadmitted_replacement(show, entry)
    show["runs"][1]["profile"] = "impl"
    show["runs"].append({"id": 6, "profile": "impl", "status": "running", "ended_at": None})
    assert not plugin._terminal_unadmitted_replacement(show, entry)


def test_reserve_recovery_rejects_workspace_bound_to_another_board(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda _: True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="a", native_run_watermark=0)
    state.activate_board("board-b", activation_id="b", native_run_watermark=0)
    first = {"board": "board-a", "task_id": "task-a", "implementation_profile": "impl",
             "reviewer_profile": "review", "workspace_path": "/shared-linked-worktree"}
    second = {"board": "board-b", "task_id": "task-b", "implementation_profile": "impl",
              "reviewer_profile": "review", "workspace_path": "/shared-linked-worktree"}
    current = state.load_state()
    current["tasks"]["board-a:task-a"] = first
    state.save_state(current)

    with pytest.raises(ValueError, match="already bound"):
        state.reserve_recovery("board-b", "task-b", failed_run_id=2, phase="implementation",
                               workspace_path="/shared-linked-worktree", checkpoint={}, binding=second, adopted=True)

    persisted = state.load_state()
    assert state.task_binding("task-b", "board-b") is None
    assert "board-b:task-b:2:implementation" not in persisted["recovery"]
    assert persisted["recovery_budgets"].get("board-b:task-b:implementation", 0) == 0


def test_adopted_recovery_rejects_unmaterialized_worktree(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda _: True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="adoption", native_run_watermark=0)
    binding = _binding()

    with pytest.raises(ValueError, match="materialized task-scoped linked worktree"):
        state.reserve_recovery("default", "task", failed_run_id=1, phase="implementation",
                               workspace_path="/work", checkpoint={}, binding=binding, adopted=True)

    assert state.task_binding("task", "default") is None
    assert state.load_state()["recovery"] == {}


def test_phase_budget_and_workspace_lease_survive_native_counter_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"; monkeypatch.setattr(state, "state_path", lambda: path); monkeypatch.setattr(state, "profile_exists", lambda _: True)
    binding = _binding(); state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {"default": {"activation_id": "a", "native_run_watermark": 0, "implementation_profile": "impl", "reviewer_profile": "review"}}})
    state.reserve_recovery("default", "task", failed_run_id=1, phase="implementation", workspace_path="/work", checkpoint={}, binding=binding, adopted=True)
    try:
        state.reserve_recovery("default", "task", failed_run_id=2, phase="implementation", workspace_path="/work", checkpoint={}, binding=binding, adopted=False)
        assert False, "new native failed run must not bypass durable phase budget"
    except ValueError as exc:
        assert "budget exhausted" in str(exc)


def test_configured_phase_budget_counts_distinct_failed_runs_across_restart_and_policy_changes(tmp_path, monkeypatch):
    """Each failed native run consumes one durable per-phase grant, never a toggle."""
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda _: True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review",
                      "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="configured-budget", native_run_watermark=0)
    state.set_recovery_policy("default", enabled=True, max_per_phase=2)
    binding = _binding()

    first = state.reserve_recovery("default", "task", failed_run_id=1, phase="implementation",
                                   workspace_path="/work", checkpoint={}, binding=binding, adopted=True)
    state.terminalize_recovery("default", "task", 1, "implementation", "native_terminal")
    # Pausing and re-enabling preserve both the configured limit and consumed grant.
    state.set_recovery_policy("default", enabled=False)
    state.set_recovery_policy("default", enabled=True)
    second = state.reserve_recovery("default", "task", failed_run_id=2, phase="implementation",
                                    workspace_path="/work", checkpoint={}, binding=binding, adopted=False)
    assert [first["failed_run_id"], second["failed_run_id"]] == [1, 2]
    state.terminalize_recovery("default", "task", 2, "implementation", "native_terminal")

    # Loading from disk is the restart boundary: terminalization did not refund.
    assert state.load_state()["recovery_budgets"]["default:task:implementation"] == 2
    state.set_recovery_policy("default", enabled=True, max_per_phase=1)
    with __import__("pytest").raises(ValueError, match="budget exhausted"):
        state.reserve_recovery("default", "task", failed_run_id=3, phase="implementation",
                               workspace_path="/work", checkpoint={}, binding=binding, adopted=False)

    # Raising the bound admits only a new failed-run identity; existing grants stay put.
    state.set_recovery_policy("default", enabled=True, max_per_phase=3)
    third = state.reserve_recovery("default", "task", failed_run_id=3, phase="implementation",
                                   workspace_path="/work", checkpoint={}, binding=binding, adopted=False)
    assert third["failed_run_id"] == 3


def test_runtime_reconciliation_recovers_lost_unblock_response_from_exact_ready_readback(monkeypatch):
    """An unknown unblock outcome is never resent once native shows the route ready."""
    binding = _binding()
    entry = {"binding": binding, "failed_run_id": 7, "workspace_path": "/work",
             "failure": {"run_id": 7, "event_id": 11, "kind": "gave_up", "payload": {}},
             "checkpoint": {"head": "a" * 40, "dirty": []}, "consumed_attempts": 1,
             "intent": {"status": "unblock_attempted"},
             "attempts": [{"implementation_profile": "terra", "reviewer_profile": "terra-review"}]}
    show = {"task": {"status": "ready", "assignee": "terra", "workspace_path": "/work"},
            "runs": [], "events": []}
    effects = []
    monkeypatch.setattr(plugin, "task_binding", lambda *_: binding)
    monkeypatch.setattr(plugin, "board_policy", lambda *_: {"runtime_escalation": {"enabled": True}})
    monkeypatch.setattr(plugin, "profile_exists", lambda _: True)
    monkeypatch.setattr(plugin, "_show", lambda *_: show)
    monkeypatch.setattr(plugin, "_runtime_failure_record", lambda *_: entry["failure"])
    monkeypatch.setattr(plugin, "_workspace_checkpoint", lambda *_: entry["checkpoint"])
    monkeypatch.setattr(plugin, "_workspace_is_exclusive", lambda *_: True)
    monkeypatch.setattr(plugin, "update_runtime_escalation_intent", lambda *_args: entry)
    monkeypatch.setattr(plugin, "_dispatch", lambda *args: effects.append(args))
    monkeypatch.setattr(plugin, "publish_runtime_escalation_routing", lambda *args, **kwargs: effects.append((args, kwargs)))

    assert plugin._reconcile_runtime_exhaustion_escalation("task", "default", entry) is True
    assert effects == [(("default", "task"), {"binding": binding})]


def test_runtime_exhaustion_escalation_requires_consumed_phase_budget_and_preserves_binding(tmp_path, monkeypatch):
    """Runtime escalation is a separately opted-in, task-scoped replacement route."""
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review", "terra", "terra-review"})
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review",
                      "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="runtime-escalation", native_run_watermark=0)
    state.set_recovery_policy("default", enabled=True, max_per_phase=2)
    state.set_runtime_escalation_policy("default", enabled=True, max_attempts=1,
                                        implementation_profile="terra", reviewer_profile="terra-review")
    with pytest.raises(ValueError, match="runtime escalation policy"):
        state.set_runtime_escalation_policy("default", enabled=True, max_attempts=2,
                                            implementation_profile="terra", reviewer_profile="terra-review")
    with pytest.raises(ValueError, match="distinct implementation"):
        state.set_runtime_escalation_policy("default", enabled=True, max_attempts=1,
                                            implementation_profile="terra", reviewer_profile="terra")
    binding = _binding()
    for failed_run_id in (2271, 2272):
        state.reserve_recovery("default", "task", failed_run_id=failed_run_id, phase="implementation",
                               workspace_path="/work", checkpoint={"head": "a" * 40, "dirty": []},
                               binding=binding, adopted=failed_run_id == 2271)
        state.terminalize_recovery("default", "task", failed_run_id, "implementation", "native_terminal")

    entry = state.reserve_runtime_escalation("default", "task", failed_run_id=2273,
                                             phase="implementation", binding=binding,
                                             checkpoint={"head": "b" * 40, "dirty": ["partial"]},
                                             failure={"run_id": 2273, "event_id": 1, "kind": "gave_up", "payload": {}},
                                             workspace_path="/work")
    assert entry["intent"]["status"] == "unblock_requested"
    assert entry["binding"] == binding
    assert state.task_binding("task", "default") == binding
    assert state.reserve_runtime_escalation("default", "task", failed_run_id=2273,
                                            phase="implementation", binding=binding,
                                            checkpoint={"head": "b" * 40, "dirty": ["partial"]},
                                            failure={"run_id": 2273, "event_id": 1, "kind": "gave_up", "payload": {}},
                                            workspace_path="/work") == entry
    unexhausted = dict(binding, task_id="unexhausted", workspace_path="/other")
    state.reserve_recovery("default", "unexhausted", failed_run_id=1, phase="implementation",
                           workspace_path="/other", checkpoint={}, binding=unexhausted, adopted=True)
    with pytest.raises(ValueError, match="not exhausted"):
        state.reserve_runtime_escalation("default", "unexhausted", failed_run_id=2,
                                         phase="implementation", binding=unexhausted, checkpoint={},
                                         failure={"run_id": 2, "event_id": 2, "kind": "gave_up", "payload": {}},
                                         workspace_path="/other")


def test_explicit_runtime_catchup_preserves_bound_history_and_durable_coder_context(tmp_path, monkeypatch):
    """An operator targets one already-bound RM03 hold; this is not a board sweep."""
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review", "terra", "terra-review"})
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review",
                      "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="catchup", native_run_watermark=0)
    state.set_recovery_policy("default", enabled=True, max_per_phase=2)
    state.set_runtime_escalation_policy("default", enabled=True, max_attempts=1,
                                        implementation_profile="terra", reviewer_profile="terra-review")
    binding = _binding()
    for failed_run_id in (1, 2):
        state.reserve_recovery("default", "task", failed_run_id=failed_run_id, phase="implementation",
                               workspace_path="/work", checkpoint={"head": "a" * 40, "dirty": []},
                               binding=binding, adopted=failed_run_id == 1)
        state.terminalize_recovery("default", "task", failed_run_id, "implementation", "native_terminal")
    failure = {"run_id": 3, "event_id": 30, "kind": "gave_up", "payload": {"budget_used": 180, "budget_max": 180}}
    context = {"original_contract": {"title": "Finish RM03", "body": "Keep the existing dirty checkpoint."},
               "reviewer_findings": [{"run_id": 9, "rationale": "Cover the rejected edge case."}],
               "latest_failure": failure,
               "checkpoint": {"head": "b" * 40, "dirty": [{"path": "partial.py", "sha256": "c" * 64}]}}

    entry = state.adopt_runtime_escalation("default", "task", failed_run_id=3, phase="implementation",
                                           binding=binding, checkpoint=context["checkpoint"], failure=failure,
                                           workspace_path="/work", coder_context=context)

    assert entry["intent"]["status"] == "catchup_requested"
    assert entry["binding"] == binding
    assert entry["coder_context"] == context
    stored = state.load_state()
    assert stored["tasks"]["default:task"] == binding
    assert stored["recovery_budgets"]["default:task:implementation"] == 2
    assert stored["recovery"]


def test_runtime_intent_requires_exact_failure_checkpoint_and_lease(tmp_path, monkeypatch):
    """A runtime route is not a generic unblock: it binds the failed event and workspace lease."""
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"; monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review", "terra", "terra-review"})
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="runtime-contract", native_run_watermark=0)
    state.set_recovery_policy("default", enabled=True, max_per_phase=1)
    state.set_runtime_escalation_policy("default", enabled=True, max_attempts=1,
                                        implementation_profile="terra", reviewer_profile="terra-review")
    binding = _binding()
    state.reserve_recovery("default", "task", failed_run_id=1, phase="implementation", workspace_path="/work",
                           checkpoint={"head": "a" * 40, "dirty": []}, binding=binding, adopted=True)
    state.terminalize_recovery("default", "task", 1, "implementation", "native_terminal")

    failure = {"run_id": 2, "event_id": 21, "kind": "gave_up", "payload": {"budget_used": 180, "budget_max": 180}}
    checkpoint = {"head": "a" * 40, "dirty": [{"path": "partial.txt", "sha256": "b" * 64}]}
    entry = state.reserve_runtime_escalation("default", "task", failed_run_id=2, phase="implementation",
                                             binding=binding, checkpoint=checkpoint, failure=failure,
                                             workspace_path="/work")
    assert entry["failure"] == failure
    assert entry["checkpoint"] == checkpoint
    assert state.load_state()["workspace_leases"]["/work"] == "default:task:2:runtime_escalation"


def test_recovery_policy_rejects_zero_boolean_fractional_negative_and_excessive_limits(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda _: True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review",
                      "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="validation", native_run_watermark=0)
    for value in (0, True, 1.5, -1, state.RECOVERY_MAX_PER_PHASE_LIMIT + 1):
        try:
            state.set_recovery_policy("default", enabled=True, max_per_phase=value)
            assert False, f"{value!r} must not be accepted as a recovery limit"
        except ValueError:
            pass


def test_verified_implementation_handoff_terminalizes_only_its_exact_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda _: True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review",
                      "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="handoff", native_run_watermark=0)
    binding = _binding()
    state.reserve_recovery("default", "task", failed_run_id=1, phase="implementation",
                           workspace_path="/work", checkpoint={}, binding=binding, adopted=True)
    state.authorize_recovery_run("default", "task", 1, "implementation", 2, "impl")
    state.pin_recovery_receipt("default", "task", 1, "implementation", 2, {"tool": "finish_implementation"})

    assert state.terminalize_implementation_handoff_recovery("default", "task", 2)
    key = state.recovery_key("default", "task", 1, "implementation")
    stored = state.load_state()
    assert stored["recovery"][key]["terminal"] == "native_review_handoff"
    assert "/work" not in stored["workspace_leases"]
    assert stored["recovery_budgets"]["default:task:implementation"] == 1


@pytest.mark.parametrize("phase, authorized_run, receipt_run", [
    ("implementation", 3, 2),
    ("implementation", 2, None),
    ("review", 2, 2),
])
def test_implementation_handoff_recovery_refuses_wrong_run_phase_or_missing_receipt(tmp_path, monkeypatch, phase, authorized_run, receipt_run):
    monkeypatch.setattr(state, "_is_materialized_linked_worktree", lambda _: True)
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda _: True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review",
                      "tasks": {}, "boards": {}})
    state.activate_board("default", activation_id="handoff-refusal", native_run_watermark=0)
    binding = _binding()
    state.reserve_recovery("default", "task", failed_run_id=1, phase=phase,
                           workspace_path="/work", checkpoint={}, binding=binding, adopted=True)
    state.authorize_recovery_run("default", "task", 1, phase, authorized_run,
                                 "review" if phase == "review" else "impl")
    if receipt_run is not None:
        state.pin_recovery_receipt("default", "task", 1, phase, authorized_run,
                                   {"tool": "finish_implementation"})
    with pytest.raises(ValueError, match="handoff recovery"):
        state.terminalize_implementation_handoff_recovery("default", "task", 2)


def test_reviewer_pretool_recovers_crash_after_native_handoff_before_ledger_finalization(monkeypatch):
    """A normal reviewer may atomically finish only the exact ended impl lease."""
    entry = {"phase": "implementation", "authorized_run_id": 2,
             "receipt": {"run_id": 2, "tool": "finish_implementation"}}
    show = {"task": {"status": "running", "current_run_id": 3, "assignee": "review"}, "runs": [
        {"id": 2, "profile": "impl", "status": "review", "outcome": "review_requested", "ended_at": 10,
         "metadata": {"local_first_review": {"implementation_run_id": 2, "implementation_profile": "impl", "reviewer_profile": "review"}}},
        {"id": 3, "profile": "review", "status": "running", "ended_at": None},
    ], "events": [
        {"kind": "review_requested", "run_id": 2, "payload": {}},
        {"kind": "claimed", "run_id": 3, "payload": {"source_status": "review"}},
    ]}
    terminalized = []
    monkeypatch.setattr(plugin, "_show", lambda _: show)
    monkeypatch.setattr(plugin, "task_binding", lambda *_: _binding())
    monkeypatch.setattr(plugin, "terminalize_implementation_handoff_recovery",
                        lambda *args: terminalized.append(args) or True)

    assert plugin._reconcile_ended_implementation_handoff_for_reviewer("task", "default", entry,
                                                                        run_id=3, profile="review")
    assert terminalized == [("default", "task", 2)]


def test_reviewer_pretool_refuses_impl_recovery_without_verified_native_handoff(monkeypatch):
    entry = {"phase": "implementation", "authorized_run_id": 2,
             "receipt": {"run_id": 2, "tool": "finish_implementation"}}
    show = {"task": {"status": "running", "current_run_id": 3, "assignee": "review"}, "runs": [
        {"id": 2, "profile": "impl", "status": "review", "outcome": "review_requested", "ended_at": 10,
         "metadata": {"local_first_review": {"implementation_run_id": 2, "implementation_profile": "impl", "reviewer_profile": "review"}}},
        {"id": 3, "profile": "review", "status": "running", "ended_at": None},
    ], "events": [{"kind": "claimed", "run_id": 3, "payload": {"source_status": "review"}}]}
    monkeypatch.setattr(plugin, "_show", lambda _: show)
    monkeypatch.setattr(plugin, "task_binding", lambda *_: _binding())
    with pytest.raises(plugin.GateError, match="exact native review transition"):
        plugin._reconcile_ended_implementation_handoff_for_reviewer("task", "default", entry,
                                                                     run_id=3, profile="review")


def test_lost_native_handoff_response_still_terminalizes_exact_recovery(monkeypatch):
    binding = _binding()
    monkeypatch.setattr(plugin, "_session", lambda: "impl-session")
    monkeypatch.setattr(plugin, "_implementation_context",
                        lambda: ("task", binding, {}, 2, "/work"))
    monkeypatch.setattr(plugin, "_candidate", lambda _: {"head": "a" * 40, "clean_tracked": True})
    monkeypatch.setattr(plugin, "_artifacts", lambda *_: [])
    monkeypatch.setattr(plugin, "_dispatch", lambda *_: (_ for _ in ()).throw(RuntimeError("lost after commit")))
    monkeypatch.setattr(plugin, "_reconcile_handoff",
                        lambda task_id, run_id: {"ok": True, "task_id": task_id, "status": "review", "reconciled": True})
    terminalized = []
    monkeypatch.setattr(plugin, "_terminalize_recovered_implementation_handoff",
                        lambda task_id, run_id: terminalized.append((task_id, run_id)) or True)

    result = plugin.finish_implementation({"summary": "Committed candidate survived a lost native response."})
    assert terminalized == [("task", 2)]
    assert '"reconciled": true' in result


def _unbound_rm02_show(task_id: str, run_id: int, *, workspace: str = "/work") -> dict:
    return {"task": {"id": task_id, "status": "blocked", "workspace_kind": "worktree",
                     "workspace_path": workspace, "assignee": "impl"},
            "runs": [{"id": run_id, "profile": "impl", "outcome": "gave_up"}],
            "events": []}


def _watchdog_recovery_mocks(monkeypatch, shows: dict[str, dict]):
    """Make exact RM02 shape selection observable without native writes."""
    policy = {"activation_id": "recovery", "native_run_watermark": 0,
              "implementation_profile": "impl", "reviewer_profile": "review",
              "recovery": {"enabled": True, "max_per_phase": 1}}
    recoveries, reservations, unblocks = {}, [], []
    monkeypatch.setattr(plugin, "board_policy", lambda _: policy)
    monkeypatch.setattr(state, "load_state", lambda: {"tasks": {}})
    monkeypatch.setattr(plugin, "_show", lambda task_id: shows[task_id])
    monkeypatch.setattr(plugin, "_allowed_terminal_failure", lambda *_: True)
    monkeypatch.setattr(plugin, "_failed_phase", lambda *_: "implementation")
    monkeypatch.setattr(plugin, "_first_unbound_gave_up",
                        lambda _policy, show: (show["runs"][0], "implementation") if show["runs"] else None)
    monkeypatch.setattr(plugin, "_workspace_checkpoint", lambda *_: {"head": "a" * 40, "dirty": []})
    monkeypatch.setattr(plugin, "profile_exists", lambda _: True)
    monkeypatch.setattr(plugin, "_workspace_is_exclusive", lambda *_: True)
    monkeypatch.setattr(plugin, "recovery_entry", lambda task_id, *_args, **_kwargs: recoveries.get(task_id))

    def reserve(board, task_id, **kwargs):
        reservations.append((board, task_id, kwargs))
        entry = {"failed_run_id": kwargs["failed_run_id"], "phase": kwargs["phase"],
                 "intent": {"status": "unblock_requested"}}
        recoveries[task_id] = entry
        return entry

    monkeypatch.setattr(plugin, "reserve_recovery", reserve)
    monkeypatch.setattr(plugin, "_dispatch",
                        lambda name, args: unblocks.append((name, args)) or {"status": "ready"})
    monkeypatch.setattr(plugin, "update_recovery_identity", lambda *_args, **_kwargs: None)
    return reservations, unblocks


def test_watchdog_refuses_ambiguous_multiple_unbound_rm02_cards(monkeypatch):
    shows = {"first": _unbound_rm02_show("first", 1), "second": _unbound_rm02_show("second", 2)}
    reservations, unblocks = _watchdog_recovery_mocks(monkeypatch, shows)
    monkeypatch.setattr(__import__("local_first_review.native", fromlist=["board_snapshot"]), "board_snapshot",
                        lambda _: [show["task"] for show in shows.values()])

    # Repeated ticks (including a restarted watchdog reading the same state)
    # must preserve ambiguity rather than draining historical failures one-by-one.
    plugin.watchdog_tick(board="default")
    plugin.watchdog_tick(board="default")

    assert reservations == []
    assert unblocks == []


def test_watchdog_adopts_one_valid_unbound_rm02_amid_unrelated_blocked_cards(monkeypatch):
    shows = {"eligible": _unbound_rm02_show("eligible", 1),
             "ordinary": {"task": {"id": "ordinary", "status": "blocked"}, "runs": [], "events": []},
             "wrong-profile": _unbound_rm02_show("wrong-profile", 2)}
    reservations, unblocks = _watchdog_recovery_mocks(monkeypatch, shows)
    monkeypatch.setattr(plugin, "_first_unbound_gave_up",
                        lambda _policy, show: (show["runs"][0], "implementation")
                        if show["runs"] and show["runs"][0]["id"] == 1 else None)
    monkeypatch.setattr(__import__("local_first_review.native", fromlist=["board_snapshot"]), "board_snapshot",
                        lambda _: [show["task"] for show in shows.values()])

    plugin.watchdog_tick(board="default")
    plugin.watchdog_tick(board="default")

    assert [task_id for _, task_id, _ in reservations] == ["eligible"]
    assert unblocks == [("kanban_unblock", {"task_id": "eligible"})]


def test_ambiguous_unbound_history_does_not_skip_bound_recovery_reconciliation(monkeypatch):
    policy = {"native_run_watermark": 0, "implementation_profile": "impl",
              "recovery": {"enabled": True, "max_per_phase": 1}}
    bound = {"board": "default", "task_id": "bound"}
    shows = {"first": _unbound_rm02_show("first", 1),
             "second": _unbound_rm02_show("second", 2),
             "bound": {"task": {"id": "bound", "status": "running"}, "runs": [], "events": []}}
    reconciled = []
    monkeypatch.setattr(plugin, "board_policy", lambda _: policy)
    monkeypatch.setattr(state, "load_state", lambda: {"tasks": {"default:bound": bound}})
    monkeypatch.setattr(plugin, "_show", lambda task_id: shows[task_id])
    monkeypatch.setattr(plugin, "_first_unbound_gave_up",
                        lambda _policy, show: (show["runs"][0], "implementation") if show["runs"] else None)
    monkeypatch.setattr(plugin, "recovery_entry",
                        lambda task_id, *_args, **_kwargs: {"task_id": task_id} if task_id == "bound" else None)
    monkeypatch.setattr(plugin, "_reconcile_recovery_claim", lambda task_id, *_args: reconciled.append(task_id))
    monkeypatch.setattr(__import__("local_first_review.native", fromlist=["board_snapshot"]), "board_snapshot",
                        lambda _: [shows["first"]["task"], shows["second"]["task"], shows["bound"]["task"]])

    plugin.watchdog_tick(board="default")

    assert reconciled == ["bound"]
