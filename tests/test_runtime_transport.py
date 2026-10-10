"""Fresh-process production transport regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys

import hermes_cli


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
# The inherited child marker must remain intact: tool lookup succeeds, then
# the real native handler refuses mutation before it can touch a board.
result = json.loads(context.dispatch_tool('kanban_unblock', {'board': 'default', 'task_id': 'held-task'}))
print('RUNTIME_TRANSPORT_RESULT=' + json.dumps(result, sort_keys=True))
""".replace("REPO_AND_CORE", repr([str(plugin_root), core_root]))
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
    assert completed.returncode == 0, completed.stderr
    line = next(line for line in completed.stdout.splitlines() if line.startswith("RUNTIME_TRANSPORT_RESULT="))
    result = json.loads(line.split("=", 1)[1])
    assert "Unknown tool" not in result.get("error", "")
    assert "unknown parameter" not in result.get("error", "").lower()
    assert "delegate_task child" in result.get("error", "").lower()
