"""Real fresh-dispatcher recovery restart regression; no provider is started."""
import json
import os
from pathlib import Path
import subprocess
import sys


def _run_dispatcher_process(code: str, env: dict[str, str]) -> dict:
    """Run one bounded, clean interpreter against the disposable board."""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=env["HERMES_HOME"],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    line = next((line for line in completed.stdout.splitlines() if line.startswith("DISPATCHER_RESTART=")), None)
    assert line is not None, completed.stdout
    return json.loads(line.split("=", 1)[1])


def test_registered_recovery_tick_does_not_duplicate_unblock_after_dispatcher_restart(tmp_path):
    """Two real dispatcher interpreters retain one native recovery grant.

    The fixture starts and terminates a local sleeping process only to produce
    the native started-run/iteration-exhaustion record.  It never starts a
    gateway, calls a provider, or treats that helper as a model worker.
    """
    home = tmp_path / "hermes"
    plugin_root = Path(__file__).resolve().parents[1]
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "local-first-review").symlink_to(plugin_root, target_is_directory=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [local-first-review]\nkanban:\n  dispatch_in_gateway: false\n"
    )
    for profile in ("impl", "review"):
        path = home / "profiles" / profile
        path.mkdir(parents=True)
        (path / "config.yaml").write_text("model:\n  default: unused-controlled-fixture\n")

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
    core_root = str(Path(__import__("hermes_cli").__file__).resolve().parent.parent)
    paths = [str(plugin_root), core_root]

    first = _run_dispatcher_process(
        """
import json, os, subprocess, sys
from pathlib import Path
sys.path[:0] = PATHS
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.kanban_db_workspace import resolve_workspace, set_workspace_path
from local_first_review import state

# This fresh process owns both native fixture construction and the first actual
# dispatcher tick.  Loading the manager is what registers the tick hook.
get_plugin_manager().discover_and_load()
kb.init_db()
conn = kbc.connect()
repo = Path(os.environ['HERMES_HOME']) / 'candidate'
repo.mkdir()
subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'fixture'], check=True)
subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'fixture@example.invalid'], check=True)
(repo / 'implementation.txt').write_text('initial\\n')
subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'fixture'], check=True)
state.save_state({'version': 1, 'implementation_profile': 'impl', 'reviewer_profile': 'review', 'tasks': {}})
state.activate_board('default', activation_id='dispatcher-restart')
state.set_recovery_policy('default', enabled=True, max_per_phase=1)
task_id = kb.create_task(conn, title='interrupted recovery fixture', assignee='impl', priority=500,
                         max_retries=1, workspace_kind='worktree', workspace_path=str(repo))
dependent = kb.create_task(conn, title='must remain gated', assignee='impl', parents=[task_id],
                           workspace_kind='worktree', workspace_path=str(repo))
task = kb.get_task(conn, task_id)
workspace = resolve_workspace(task, board='default')
set_workspace_path(conn, task_id, workspace)
worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
try:
    started = kbd.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: worker.pid,
                                max_spawn=1, reconcile_orphans=False)
    assert started.spawned == [(task_id, 'impl', str(workspace))], started
    worker.terminate()
    worker.wait(timeout=10)
    assert kbd._record_task_failure(
        conn, task_id, 'Iteration budget exhausted (180/180)', outcome='timed_out',
        release_claim=True, end_run=True, event_payload_extra={'budget_used': 180, 'budget_max': 180},
    )
finally:
    if worker.poll() is None:
        worker.kill()
        worker.wait(timeout=10)
# max_spawn=0 deliberately proves an actual dispatcher tick without pretending
# to start a replacement worker.  The registered plugin hook performs native
# recovery through its PluginContext transport.
tick = kbd.dispatch_once(conn, max_spawn=0, reconcile_orphans=False)
show = {'task': kb.get_task(conn, task_id), 'dependent': kb.get_task(conn, dependent),
        'events': kb.list_events(conn, task_id)}
assert not tick.spawned
assert show['task'].status in {'ready', 'todo'}
assert show['dependent'].status == 'todo'
ledger = state.load_state()
print('DISPATCHER_RESTART=' + json.dumps({
    'task_id': task_id, 'dependent': dependent,
    'unblocks': sum(event.kind == 'unblocked' for event in show['events']),
    'budget': ledger['recovery_budgets']['default:' + task_id + ':implementation'],
    'status': show['task'].status, 'dependent_status': show['dependent'].status,
    'recovery': ledger['recovery'],
}, sort_keys=True))
conn.close()
""".replace("PATHS", repr(paths)),
        env,
    )
    assert first["unblocks"] == 1
    assert first["budget"] == 1
    assert first["dependent_status"] == "todo"
    assert len(first["recovery"]) == 1

    second = _run_dispatcher_process(
        """
import json, sys
sys.path[:0] = PATHS
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.plugins import get_plugin_manager
from local_first_review import state
get_plugin_manager().discover_and_load()
conn = kbc.connect()
# This is a second, fresh dispatcher process against the same board and ledger.
# It runs the registered recovery hook but does not impersonate a worker spawn.
tick = kbd.dispatch_once(conn, max_spawn=0, reconcile_orphans=False)
show = {'task': kb.get_task(conn, TASK_ID), 'dependent': kb.get_task(conn, DEPENDENT),
        'events': kb.list_events(conn, TASK_ID)}
assert not tick.spawned
assert show['task'].status in {'ready', 'todo'}
assert show['dependent'].status == 'todo'
ledger = state.load_state()
print('DISPATCHER_RESTART=' + json.dumps({
    'unblocks': sum(event.kind == 'unblocked' for event in show['events']),
    'budget': ledger['recovery_budgets']['default:' + TASK_ID + ':implementation'],
    'status': show['task'].status, 'dependent_status': show['dependent'].status,
    'recovery': ledger['recovery'],
}, sort_keys=True))
conn.close()
""".replace("PATHS", repr(paths)).replace("TASK_ID", repr(first["task_id"])).replace("DEPENDENT", repr(first["dependent"])),
        env,
    )

    assert second["unblocks"] == first["unblocks"] == 1
    assert second["budget"] == first["budget"] == 1
    assert second["recovery"] == first["recovery"]
    assert second["status"] == first["status"]
    assert second["dependent_status"] == first["dependent_status"] == "todo"
