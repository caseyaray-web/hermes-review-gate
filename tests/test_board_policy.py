import json
import subprocess

import pytest

from local_first_review import state


@pytest.mark.parametrize("number", [1, 2, 3])
def test_v5_migration_preserves_retained_attempt_and_consumed_budget(policy_file, number):
    binding = {"board": "board-a", "task_id": "task", "implementation_profile": "impl",
               "reviewer_profile": "review", "workspace_path": "/work"}
    old = {"board": "board-a", "task_id": "task", "binding": binding, "attempt": number,
           "implementation_profile": "strong", "reviewer_profile": "post-review",
           "intent": {"status": "routed", "review_run_id": 7, "candidate": {"head": "a"}, "change_count": 2}}
    data = state._empty_state()
    data.update(version=5, escalations={"board-a:task": old})
    policy_file.write_text(json.dumps(data))
    ledger = state.load_state()["escalations"]["board-a:task"]
    assert ledger["consumed_attempts"] == ledger["current_attempt"] == number
    assert len(ledger["attempts"]) == 1  # Missing historical evidence must not be invented.
    assert state.current_escalation_attempt(ledger)["intent"] == old["intent"]


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "local-first-review.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review"})
    return path


def native_task(*, task_id="future", status="running", assignee="impl", workspace="/work", workspace_kind="worktree"):
    return {"id": task_id, "status": status, "assignee": assignee,
            "workspace_kind": workspace_kind, "workspace_path": workspace}


def native_run(*, run_id=9, profile="impl", started_at=101, ended_at=None):
    return {"id": run_id, "profile": profile, "status": "running", "started_at": started_at, "ended_at": ended_at}


def test_board_activation_and_first_binding_allow_same_profile_for_both_roles(policy_file):
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "impl", "tasks": {}, "boards": {}})

    policy = state.activate_board("board-a", activation_id="same-profile", native_run_watermark=8)
    binding = state.bind_first_owned_run("board-a", native_task(), [native_run()], run_id=9, profile="impl")

    assert policy["implementation_profile"] == policy["reviewer_profile"] == "impl"
    assert binding["implementation_profile"] == binding["reviewer_profile"] == "impl"


def test_activation_migrates_legacy_state_and_first_future_run_is_bound(policy_file):
    policy_file.write_text(json.dumps({"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "old:legacy": {"board": "old", "task_id": "legacy", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/legacy"}}}))

    policy = state.activate_board("board-a", activation_id="activation-a", native_run_watermark=8)
    binding = state.bind_first_owned_run("board-a", native_task(), [native_run()], run_id=9, profile="impl")

    persisted = state.load_state()
    assert persisted["version"] == 6
    assert persisted["tasks"]["old:legacy"]["workspace_path"] == "/legacy"
    assert policy == persisted["boards"]["board-a"]
    assert binding["policy_activation_id"] == "activation-a"
    assert binding["policy_native_run_watermark"] == 8
    assert binding["native_run_id"] == 9
    assert state.task_binding("future", "board-a") == binding


@pytest.mark.parametrize("task,runs,run_id,profile", [
    (native_task(workspace_kind="dir"), [native_run()], 9, "impl"),
    (native_task(status="review"), [native_run()], 9, "impl"),
    (native_task(), [native_run(run_id=8)], 8, "impl"),
    (native_task(), [native_run(), native_run(run_id=10, ended_at=102)], 9, "impl"),
    (native_task(assignee="other"), [native_run()], 9, "impl"),
    (native_task(workspace="relative"), [native_run()], 9, "impl"),
    (native_task(), [native_run(profile="other")], 9, "other"),
])
def test_first_binding_refuses_attention_or_mismatch(policy_file, task, runs, run_id, profile):
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="activation-a", native_run_watermark=8)

    with pytest.raises(ValueError):
        state.bind_first_owned_run("board-a", task, runs, run_id=run_id, profile=profile)

    assert state.load_state()["tasks"] == {}


def test_first_binding_rejects_a_workspace_already_bound_to_another_task(policy_file):
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="activation-a", native_run_watermark=8)
    state.bind_first_owned_run("board-a", native_task(task_id="one"), [native_run()], run_id=9, profile="impl")

    with pytest.raises(ValueError, match="already bound"):
        state.bind_first_owned_run("board-a", native_task(task_id="two"), [native_run(run_id=10)], run_id=10, profile="impl")

    assert state.task_binding("two", "board-a") is None


def test_first_binding_rejects_workspace_reuse_across_boards(policy_file):
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="activation-a", native_run_watermark=8)
    state.activate_board("board-b", activation_id="activation-b", native_run_watermark=8)
    state.bind_first_owned_run("board-a", native_task(task_id="one"), [native_run()], run_id=9, profile="impl")

    with pytest.raises(ValueError, match="already bound"):
        state.bind_first_owned_run("board-b", native_task(task_id="two"), [native_run(run_id=10)], run_id=10, profile="impl")

    assert state.task_binding("two", "board-b") is None


def test_enrollment_rejects_repo_root_before_worktree_materialization(policy_file, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "tracked.txt").write_text("fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    task = {"id": "unmaterialized", "status": "blocked", "block_kind": "needs_input", "assignee": "impl",
            "workspace_kind": "worktree", "workspace_path": str(repo)}

    with pytest.raises(ValueError, match="materialized linked worktree"):
        state.enroll_task(board="board-a", task=task, runs=[])

    assert state.task_binding("unmaterialized", "board-a") is None


def test_malformed_board_policy_fails_closed(policy_file):
    policy_file.write_text(json.dumps({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {},
                                       "boards": {"board-a": {"activation_id": "", "native_run_watermark": "nope", "implementation_profile": "impl", "reviewer_profile": "review"}}}))

    with pytest.raises(ValueError, match="board policy"):
        state.load_state()


def test_escalation_policy_allows_same_profile_selection_and_persists(policy_file, monkeypatch):
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review", "strong", "post-review"})
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="escalation", native_run_watermark=0)

    policy = state.set_escalation_policy("board-a", enabled=True, normal_correction_limit=1,
                                         max_attempts=1, implementation_profile="strong", reviewer_profile="post-review")

    assert policy["escalation"] == {"enabled": True, "normal_correction_limit": 1, "max_attempts": 1,
                                     "implementation_profile": "strong", "reviewer_profile": "post-review"}
    same_profile = state.set_escalation_policy("board-a", enabled=True, normal_correction_limit=1,
                                               max_attempts=1, implementation_profile="strong", reviewer_profile="strong")
    assert same_profile["escalation"]["implementation_profile"] == same_profile["escalation"]["reviewer_profile"] == "strong"
    disabled = state.set_escalation_policy("board-a", enabled=False, normal_correction_limit=1, max_attempts=1)
    assert disabled["escalation"]["enabled"] is False


def test_escalation_intent_and_effective_routing_survive_restart_without_rewriting_binding(policy_file, monkeypatch):
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review", "strong", "post-review"})
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="escalation-intent", native_run_watermark=0)
    state.set_escalation_policy("board-a", enabled=True, normal_correction_limit=1, max_attempts=1,
                                implementation_profile="strong", reviewer_profile="strong")
    binding = {"board": "board-a", "task_id": "task", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/work"}
    state.save_state({**state.load_state(), "tasks": {"board-a:task": binding}})

    entry = state.reserve_escalation("board-a", "task", review_run_id=7, binding=binding,
                                     candidate={"head": "a" * 40, "clean_tracked": True}, change_count=1)
    assert entry["attempts"][0]["intent"]["status"] == "changes_requested_pending"
    assert state.effective_routing("task", "board-a") is None
    route = state.finalize_escalation_routing("board-a", "task", review_run_id=7)

    reloaded = state.load_state()
    assert route["implementation_profile"] == "strong"
    assert reloaded["tasks"]["board-a:task"] == binding
    assert reloaded["effective_routing"]["board-a:task"]["reviewer_profile"] == "strong"
    with pytest.raises(ValueError, match="attempts exhausted"):
        state.reserve_escalation("board-a", "task", review_run_id=8, binding=binding,
                                 candidate={"head": "b" * 40, "clean_tracked": True}, change_count=2)


def test_escalation_attempts_are_append_only_and_keep_the_current_selector(policy_file, monkeypatch):
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review", "strong", "post-review"})
    state.save_state({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {}})
    state.activate_board("board-a", activation_id="append-only", native_run_watermark=0)
    state.set_escalation_policy("board-a", enabled=True, normal_correction_limit=1, max_attempts=2,
                                implementation_profile="review", reviewer_profile="post-review")
    binding = {"board": "board-a", "task_id": "task", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/work"}
    state.save_state({**state.load_state(), "tasks": {"board-a:task": binding}})

    first = state.reserve_escalation("board-a", "task", review_run_id=7, binding=binding,
                                     candidate={"head": "a" * 40}, change_count=1)
    state.finalize_escalation_routing("board-a", "task", review_run_id=7)
    first = state.load_state()["escalations"]["board-a:task"]
    second = state.reserve_escalation("board-a", "task", review_run_id=9, binding=binding,
                                      candidate={"head": "b" * 40}, change_count=2)

    ledger = state.load_state()["escalations"]["board-a:task"]
    assert ledger["consumed_attempts"] == 2
    assert ledger["current_attempt"] == 2
    assert [attempt["intent"]["review_run_id"] for attempt in ledger["attempts"]] == [7, 9]
    assert ledger["attempts"][0] == first["attempts"][0]
    assert ledger["attempts"][1] == second["attempts"][1]
    with pytest.raises(ValueError, match="attempts exhausted"):
        state.reserve_escalation("board-a", "task", review_run_id=11, binding=binding,
                                 candidate={"head": "c" * 40}, change_count=3)
