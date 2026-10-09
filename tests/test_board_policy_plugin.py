import pytest

from local_first_review import plugin


def test_direct_lifecycle_on_board_policy_binds_eligible_run_then_refuses(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "future")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_PROFILE", "impl")
    monkeypatch.setattr(plugin, "is_managed", lambda task_id, board: True)
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: None)
    monkeypatch.setattr(plugin, "_show", lambda task_id: {"task": {"id": task_id}, "runs": []})
    bound = []
    monkeypatch.setattr(plugin, "_binding", lambda show: bound.append(show) or ("future", {"implementation_profile": "impl", "reviewer_profile": "review"}))

    result = plugin.guard("kanban_complete", {"summary": "bypass"})

    assert bound == [{"task": {"id": "future"}, "runs": []}]
    assert result and result["action"] == "block"
    assert "finish_implementation" in result["message"]


@pytest.mark.parametrize("tool", ["kanban_complete", "kanban_request_review", "kanban_request_changes"])
def test_board_policy_direct_lifecycle_refuses_attention_when_binding_is_ineligible(monkeypatch, tool):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "old")
    monkeypatch.setattr(plugin, "is_managed", lambda task_id, board: True)
    monkeypatch.setattr(plugin, "recovery_entry", lambda *_: None)
    monkeypatch.setattr(plugin, "_show", lambda task_id: {"task": {"id": task_id}, "runs": [{"id": 1}]})
    monkeypatch.setattr(plugin, "_binding", lambda show: (_ for _ in ()).throw(plugin.GateError("prior run")))

    result = plugin.guard(tool, {})

    assert result and result["action"] == "block"
    assert "operator attention" in result["message"]
