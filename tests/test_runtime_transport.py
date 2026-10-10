"""Fresh-process production transport regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys

import hermes_cli
import pytest


def test_fresh_cli_context_registers_native_unblock_after_registry_cached(tmp_path):
    """The review plugin owns its native-tool dependency; dispatcher startup does not preload it."""
    home = tmp_path / "hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [local-first-review]\nkanban:\n  dispatch_in_gateway: false\n"
    )
    plugin_root = Path(__file__).resolve().parents[1]
    (home / "plugins" / "local-first-review").symlink_to(plugin_root, target_is_directory=True)
    core_root = str(Path(hermes_cli.__file__).resolve().parent.parent)
    code = """
import json
import sqlite3
import sys
from pathlib import Path
sys.path[:0] = REPO_AND_CORE
from tools.registry import registry
assert 'tools.kanban_tools' not in sys.modules
import hermes_cli.kanban_db_dispatch
assert 'tools.kanban_tools' not in sys.modules
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
from local_first_review import plugin
assert 'tools.kanban_tools' in sys.modules
context = plugin._context()
assert context is plugin._context()
from tools.kanban_tools import _is_delegated_child_context
if _is_delegated_child_context():
    # Preserve the inherited guard. Children prove registration, not effects.
    result = json.loads(context.dispatch_tool('kanban_unblock', {'board': 'default', 'task_id': 'held-task'}))
    assert 'delegate_task child' in result.get('error', '').lower(), result
    mode = 'guarded-child'
else:
    # Parent execution proves the real transport against an isolated board.
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    assert kb.kanban_db_path().is_relative_to(Path(HOME_PATH))
    try:
        kbc.init_db()
    except sqlite3.OperationalError as exc:
        # This isolated child cannot initialize the parent-owned native board.
        # Do not clear its guard or substitute a fake transport proof.
        print('RUNTIME_TRANSPORT_FENCED=' + str(exc))
        raise SystemExit(75)
    conn = kbc.connect()
    task_id = kb.create_task(conn, title='Disposable transport proof', assignee='default', initial_status='blocked')
    result = json.loads(context.dispatch_tool('kanban_unblock', {'board': 'default', 'task_id': task_id}))
    assert result.get('status') in {'todo', 'ready'}, result
    assert kb.get_task(conn, task_id).status == result['status']
    assert len([e for e in kb.list_events(conn, task_id) if e.kind == 'unblocked']) == 1
    kb.archive_task(conn, task_id)
    conn.close()
    mode = 'native-parent'
print('RUNTIME_TRANSPORT_RESULT=' + json.dumps({'mode':mode, 'result':result}, sort_keys=True))
""".replace("REPO_AND_CORE", repr([str(plugin_root), core_root])).replace("HOME_PATH", repr(str(home)))
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("HERMES_KANBAN_", "HERMES_PROFILE", "HERMES_SESSION")):
            env.pop(name)
    env.update({
        "HOME": str(tmp_path / "os-home"),
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
    })
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode == 75:
        pytest.skip('fresh isolated process cannot initialize the parent-owned native board; native transport proof remains pending')
    assert completed.returncode == 0, completed.stderr
    line = next(line for line in completed.stdout.splitlines() if line.startswith("RUNTIME_TRANSPORT_RESULT="))
    evidence = json.loads(line.split("=", 1)[1])
    result = evidence['result']
    assert "Unknown tool" not in result.get("error", "")
    assert "unknown parameter" not in result.get("error", "").lower()
    from tools.kanban_tools import _is_delegated_child_context
    assert evidence['mode'] == ('guarded-child' if _is_delegated_child_context() else 'native-parent')
