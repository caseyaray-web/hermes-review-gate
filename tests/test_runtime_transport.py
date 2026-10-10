"""Production-shape PluginContext transport regression tests."""
import json

from test_review_gate import board
from local_first_review import plugin


def test_runtime_unblock_uses_actual_plugin_context_with_board_routing(board):
    """Installed Hermes rejects the extra board arg and accepts the canonical call."""
    b = board
    task_id = b.kb.create_task(
        b.conn,
        title="Runtime transport unblock",
        assignee="impl",
        initial_status="blocked",
        workspace_kind="worktree",
        workspace_path=str(b.repo_root),
    )
    b.clear()  # Gateway/watchdog shape: no dispatcher-owned worker environment.

    context = plugin._context()
    rejected = json.loads(context.dispatch_tool(
        "kanban_unblock", {"board": "default", "task_id": task_id}
    ))
    assert "unknown parameter(s): board" in rejected["error"]
    assert b.show(task_id)["task"]["status"] == "blocked"

    result = json.loads(context.dispatch_tool("kanban_unblock", {"task_id": task_id}))
    assert result["ok"] is True
    assert result["task_id"] == task_id
    assert result["status"] == "ready"
    assert b.show(task_id)["task"]["status"] == "ready"
