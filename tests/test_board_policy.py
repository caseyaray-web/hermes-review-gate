import json

import pytest

from local_first_review import state


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "local-first-review.json"
    monkeypatch.setattr(state, "state_path", lambda: path)
    monkeypatch.setattr(state, "profile_exists", lambda name: name in {"impl", "review"})
    return path


def native_task(*, task_id="future", status="running", assignee="impl", workspace="/work"):
    return {"id": task_id, "status": status, "assignee": assignee,
            "workspace_kind": "dir", "workspace_path": workspace}


def native_run(*, run_id=9, profile="impl", started_at=101, ended_at=None):
    return {"id": run_id, "profile": profile, "status": "running", "started_at": started_at, "ended_at": ended_at}


def test_activation_migrates_legacy_state_and_first_future_run_is_bound(policy_file):
    policy_file.write_text(json.dumps({"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "old:legacy": {"board": "old", "task_id": "legacy", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/legacy"}}}))

    policy = state.activate_board("board-a", activation_id="activation-a", native_run_watermark=8)
    binding = state.bind_first_owned_run("board-a", native_task(), [native_run()], run_id=9, profile="impl")

    persisted = state.load_state()
    assert persisted["version"] == 4
    assert persisted["tasks"]["old:legacy"]["workspace_path"] == "/legacy"
    assert policy == persisted["boards"]["board-a"]
    assert binding["policy_activation_id"] == "activation-a"
    assert binding["policy_native_run_watermark"] == 8
    assert binding["native_run_id"] == 9
    assert state.task_binding("future", "board-a") == binding


@pytest.mark.parametrize("task,runs,run_id,profile", [
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


def test_malformed_board_policy_fails_closed(policy_file):
    policy_file.write_text(json.dumps({"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {},
                                       "boards": {"board-a": {"activation_id": "", "native_run_watermark": "nope", "implementation_profile": "impl", "reviewer_profile": "review"}}}))

    with pytest.raises(ValueError, match="board policy"):
        state.load_state()
