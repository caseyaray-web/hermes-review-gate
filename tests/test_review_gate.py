"""Native controlled fixtures. These do not claim provider-backed worker rehearsal."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from local_first_review import plugin, state
from local_first_review.native import snapshot


@pytest.fixture
def board(tmp_path, monkeypatch):
    from agent.delegation_context import is_delegated_child_context, kanban_path_is_fenced
    if is_delegated_child_context() or kanban_path_is_fenced(tmp_path / 'hermes' / 'kanban.db'):
        pytest.skip('native mutation proof is parent-owned; delegated guard remains intact')
    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    for name in list(os.environ):
        if name.startswith(('HERMES_KANBAN_', 'HERMES_PROFILE', 'HERMES_SESSION')):
            monkeypatch.delenv(name)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    (home / 'config.yaml').write_text('plugins:\n  enabled: [local-first-review]\nkanban:\n  dispatch_in_gateway: false\n')
    for name in ('impl', 'review', 'other'):
        profile = home / 'profiles' / name
        profile.mkdir(parents=True)
        (profile / 'config.yaml').write_text('model:\n  default: unused-controlled-fixture\n')
    directory = home / 'plugins'
    directory.mkdir()
    (directory / 'local-first-review').symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
    from hermes_cli.plugins import get_plugin_manager
    import tools.kanban_tools
    from model_tools import handle_function_call
    assert kb.kanban_db_path().is_relative_to(home)
    assert state.profiles() == ['default', 'impl', 'other', 'review']
    get_plugin_manager().discover_and_load()
    kb.init_db()
    conn = kbc.connect()
    repo = home / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'Review fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / 'implementation.txt').write_text('version one\n')
    (repo / '.gitignore').write_text('check.log\n')
    git('add', '.')
    git('commit', '-qm', 'fixture candidate')
    state.save_state({'version':1, 'implementation_profile':'impl', 'reviewer_profile':'review', 'tasks':{}})
    pred = kb.create_task(conn, title='Managed implementation', assignee='impl', initial_status='blocked', workspace_kind='dir', workspace_path=str(repo))
    assert kb.block_task(conn, pred, reason='Awaiting explicit review enrollment', kind='needs_input')
    observed = snapshot('default', pred)
    state.enroll_task(board='default', task=observed['task'], runs=observed['runs'])
    child = kb.create_task(conn, title='Gated successor', assignee='impl', parents=[pred], workspace_kind='dir', workspace_path=str(repo))
    assert kb.unblock_task(conn, pred)
    class Board:
        def clear(self):
            for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_PROFILE','HERMES_SESSION_ID'):
                monkeypatch.delenv(key, raising=False)
        def dispatch(self, expected=None):
            self.clear()
            result=kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: os.getpid(), max_spawn=1, reconcile_orphans=False)
            assert len(result.spawned)==1, dataclasses.asdict(result)
            task=kb.get_task(conn,result.spawned[0][0])
            if expected is not None:
                assert task.id == expected
            monkeypatch.setenv('HERMES_KANBAN_TASK',task.id)
            monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(task.current_run_id))
            monkeypatch.setenv('HERMES_PROFILE',task.assignee)
            monkeypatch.setenv('HERMES_SESSION_ID','controlled-'+str(task.current_run_id))
            return task
        def call(self, name, **args):
            return json.loads(handle_function_call(name,args,task_id='agent-session-not-card-id'))
        def show(self, task_id=None):return snapshot('default',task_id or pred)
        def gated(self):
            assert kb.get_task(conn,child).status=='todo'
            assert not any(e.kind=='completed' for e in kb.list_events(conn,pred))
    b=Board()
    b.kb,b.kbd,b.conn,b.repo,b.pred,b.child,b.git,b.home=kb,kbd,conn,repo,pred,child,git,home
    try:
        yield b
    finally:
        b.clear()
        for task in kb.list_tasks(conn,include_archived=True):
            if task.status=='running':
                kb.block_task(conn,task.id,reason='fixture finished',kind='needs_input',expected_run_id=task.current_run_id)
            elif task.status in ('ready','todo','review'):
                kb.archive_task(conn,task.id)
        assert not any(t.status in ('ready','running','review') for t in kb.list_tasks(conn))
        conn.close()


def test_registered_dependency_correction_and_approval(board, monkeypatch):
    b=board
    b.dispatch(); b.gated()
    before=b.show()['events']
    assert 'finish_implementation' in b.call('kanban_complete',summary='must not bypass')['error']
    assert b.show()['events']==before
    (b.repo/'check.log').write_text('checks: passed\n')
    first=b.call('finish_implementation',summary='Implemented tracked candidate; check.log records checks.',artifacts=['check.log'])
    assert first['ok'], first
    b.gated()
    assert b.show()['task']['status']=='review'
    # Duplicate finishing never emits another handoff.
    assert b.call('finish_implementation',summary='duplicate')['error']
    assert len([r for r in b.show()['runs'] if r['outcome']=='review_requested'])==1
    task=b.dispatch(); assert task.assignee=='review'; b.gated()
    wrong=b.call('finish_implementation',summary='reviewer cannot implement')
    assert wrong['error']
    monkeypatch.setenv('HERMES_PROFILE','other')
    assert b.call('submit_review',verdict='approved',rationale='wrong reviewer')['error']
    monkeypatch.setenv('HERMES_PROFILE','review')
    changed=b.call('submit_review',verdict='changes_requested',rationale='Add the missing second-version behavior and rerun checks.')
    assert changed['ok'],changed
    b.gated()
    assert b.dispatch().assignee=='impl'
    (b.repo/'implementation.txt').write_text('version two\n')
    b.git('add','.');b.git('commit','-qm','correction candidate')
    assert b.call('finish_implementation',summary='Correction committed; checks passed.',artifacts=['check.log'])['ok']
    assert b.dispatch().assignee=='review';b.gated()
    # Current candidate and artifact drift both refuse completion.
    (b.repo/'implementation.txt').write_text('unstaged drift\n')
    assert b.call('submit_review',verdict='approved',rationale='must reject stale candidate')['error']
    b.git('restore','implementation.txt')
    (b.repo/'check.log').write_text('changed check evidence\n')
    assert b.call('submit_review',verdict='approved',rationale='must reject stale artifact')['error']
    (b.repo/'check.log').write_text('checks: passed\n')
    accepted=b.call('submit_review',verdict='approved',rationale='Compared both candidate revisions and check evidence; required behavior is correct.')
    assert accepted['ok'],accepted
    assert b.show()['task']['status']=='done'
    assert b.show()['runs'][-1]['metadata']['local_first_review']['verdict']=='approved'
    assert b.dispatch().id==b.child


def test_native_board_policy_watermark_binds_never_run_and_future_cards(board):
    """Controlled native claims prove the run-ID boundary without a provider."""
    b = board
    old = b.kb.create_task(b.conn, title='Pre-activation history', assignee='impl', priority=300,
                           workspace_kind='dir', workspace_path=str(b.repo))
    assert b.dispatch(expected=old).id == old
    old_run = b.kb.get_task(b.conn, old).current_run_id
    existing = b.kb.create_task(b.conn, title='Existing never-run managed card', assignee='impl', priority=200,
                                workspace_kind='dir', workspace_path=str(b.repo))

    policy = state.activate_board('default', activation_id='native-watermark')
    assert policy['native_run_watermark'] >= old_run
    with pytest.raises(ValueError, match='watermark'):
        state.bind_first_owned_run('default', *[b.show(old)[key] for key in ('task', 'runs')], run_id=old_run, profile='impl')
    assert b.kb.block_task(b.conn, old, reason='old run for activation classification', kind='needs_input', expected_run_id=old_run)
    b.clear()

    future = b.kb.create_task(b.conn, title='Future managed card', assignee='impl', priority=400,
                              workspace_kind='dir', workspace_path=str(b.repo))
    assert b.dispatch(expected=future).id == future
    # The registered pre-tool hook binds the exact future native run, then
    # blocks the bypass before native completion changes state.
    assert b.call('kanban_complete', summary='direct completion is forbidden')['error']
    future_binding = state.task_binding(future, 'default')
    assert future_binding and future_binding['native_run_id'] > policy['native_run_watermark']
    assert b.kb.block_task(b.conn, future, reason='future binding proven', kind='needs_input',
                           expected_run_id=b.kb.get_task(b.conn, future).current_run_id)
    b.clear()

    dependent = b.kb.create_task(b.conn, title='Policy gated successor', assignee='impl', priority=200,
                                 parents=[existing], workspace_kind='dir', workspace_path=str(b.repo))
    assert b.dispatch(expected=existing).id == existing
    assert b.call('finish_implementation', summary='Existing never-run card is now a clean candidate.')['ok']
    binding = state.task_binding(existing, 'default')
    assert binding and binding['native_run_id'] > policy['native_run_watermark']
    assert b.kb.get_task(b.conn, dependent).status == 'todo'
    assert b.dispatch(expected=existing).assignee == 'review'
    assert b.call('submit_review', verdict='changes_requested', rationale='Add the required correction.')['ok']
    assert b.dispatch(expected=existing).assignee == 'impl'
    (b.repo / 'implementation.txt').write_text('policy correction\n')
    b.git('add', '.'); b.git('commit', '-qm', 'policy correction')
    assert b.call('finish_implementation', summary='Corrected candidate committed and checked.')['ok']
    assert b.dispatch(expected=existing).assignee == 'review'
    assert b.call('submit_review', verdict='approved', rationale='Correction and candidate are approved.')['ok']
    assert b.kb.get_task(b.conn, existing).status == 'done'
    assert b.dispatch(expected=dependent).id == dependent


def test_native_board_policy_provenance_isolated_by_board(board, monkeypatch):
    """A named-board native claim cannot consume the default board's policy."""
    from hermes_cli import kanban_db_connect as kbc
    b = board
    b.kb.create_board('other')
    other = kbc.connect(board='other')
    try:
        with b.kb.scoped_current_board('other'):
            task_id = b.kb.create_task(other, title='Other board card', assignee='impl', initial_status='blocked',
                                       workspace_kind='dir', workspace_path=str(b.repo))
            assert b.kb.unblock_task(other, task_id)
            other_policy = state.activate_board('other', activation_id='other-policy')
            result = b.kbd.dispatch_once(other, spawn_fn=lambda *a, **k: os.getpid(), max_spawn=1, reconcile_orphans=False)
            assert result.spawned and result.spawned[0][0] == task_id
            observed = snapshot('other', task_id)
            run_id = observed['task']['current_run_id']
            binding = state.bind_first_owned_run('other', observed['task'], observed['runs'], run_id=run_id, profile='impl')
        assert binding['board'] == 'other'
        assert binding['native_run_id'] > other_policy['native_run_watermark']
        assert state.task_binding(task_id, 'default') is None
        assert state.board_policy('default') is None
        assert state.board_policy('other')['activation_id'] == 'other-policy'
    finally:
        other.close()


def test_correction_limit_from_native_runs(board):
    b=board
    for cycle in range(3):
        assert b.dispatch().assignee=='impl'
        assert b.call('finish_implementation',summary=f'Candidate attempt {cycle}: source and checks reviewed.')['ok']
        assert b.dispatch().assignee=='review'
        result=b.call('submit_review',verdict='changes_requested',rationale=f'Missing required behavior {cycle}; implement and verify it.')
        assert result['ok'],result
        b.gated()
    show=b.show()
    assert show['task']['status'] in ('blocked', 'triage')
    assert show['task']['block_kind']=='needs_input'
    assert len([r for r in show['runs'] if r['outcome']=='changes_requested'])==2
    assert 'Missing required behavior 2' in show['runs'][-1]['summary']


def test_ambiguous_handoff_native_readback_and_restart(board,monkeypatch):
    b=board;b.dispatch()
    original=plugin._dispatch
    def lose_response(name,args):
        result=original(name,args)
        if name=='kanban_request_review':raise RuntimeError('injected lost response after native commit')
        return result
    monkeypatch.setattr(plugin,'_dispatch',lose_response)
    result=b.call('finish_implementation',summary='Candidate checks passed before deliberately lost response.')
    assert result['ok'] and result['reconciled'],result
    monkeypatch.setattr(plugin,'_dispatch',original)
    # A fresh interpreter must reconcile the persisted handoff without replaying it.
    import sys
    import hermes_cli
    core_root = str(Path(hermes_cli.__file__).resolve().parent.parent)
    code = (f"import sys,json;sys.path[:0]={[str(Path(__file__).resolve().parents[1]), core_root]!r};"
            "import tools.kanban_tools;from hermes_cli.plugins import get_plugin_manager;get_plugin_manager().discover_and_load();"
            "from local_first_review.plugin import _reconcile_handoff;"
            f"print(json.dumps(_reconcile_handoff({b.pred!r},{int(os.environ['HERMES_KANBAN_RUN_ID'])})))")
    restarted = subprocess.run([sys.executable,'-I','-B','-c',code],cwd=b.repo,env=dict(os.environ),capture_output=True,text=True,timeout=45)
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout)['reconciled'] is True
    assert b.call('finish_implementation',summary='retry after restart observation')['error']
    assert len([r for r in b.show()['runs'] if r['outcome']=='review_requested'])==1
    assert b.dispatch().assignee=='review'
    assert b.call('submit_review',verdict='approved',rationale='Read native handoff and rechecked unchanged candidate after lost response.')['ok']


def test_reviewer_failure_holds_successor(board):
    b = board
    state.activate_board('default', activation_id='manual-hold')
    state.set_recovery_policy('default', enabled=True)
    b.dispatch()
    assert b.call('finish_implementation', summary='Candidate committed and checks passed before reviewer interruption.')['ok']
    review = b.dispatch()
    assert review.assignee == 'review'
    assert b.kb.block_task(b.conn, b.pred, reason='Controlled reviewer failure; operator inspection required', kind='needs_input', expected_run_id=review.current_run_id)
    # A needs_input hold is a human decision even when native repeated-hold
    # routing reports triage.  The watchdog must never turn it into a retry.
    held = b.show()
    assert held['task']['status'] in {'blocked', 'triage'}
    plugin.watchdog_tick(board='default')
    assert b.show()['task']['status'] == held['task']['status']
    assert state.recovery_entry(b.pred, 'default') is None
    b.gated()
    assert b.call('submit_review', verdict='approved', rationale='Late verdict from ended failed reviewer must not complete.')['error']
    b.gated()



def test_joined_reviewer_recovery_admits_native_promoted_reviewer_without_fabricated_unblock(board, monkeypatch):
    """Started reviewer exhaustion is admitted from its exact native replacement claim."""
    import sys
    import hermes_cli

    b = board
    state.activate_board('default', activation_id='reviewer-recovery')
    state.set_recovery_policy('default', enabled=True)
    assert b.dispatch().assignee == 'impl'
    assert b.call('finish_implementation', summary='Committed candidate ready for reviewer recovery proof.')['ok']

    # Start a real disposable review worker, then report the same bounded
    # iteration exhaustion as RM02 through the native terminal-failure producer.
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        started = b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: worker.pid,
                                      max_spawn=1, reconcile_orphans=False)
        assert started.spawned == [(b.pred, 'review', str(b.repo))]
        worker.terminate(); worker.wait(timeout=10)
        assert b.kbd._record_task_failure(
            b.conn, b.pred,
            'Iteration budget exhausted (180/180) — task could not complete within the allowed iterations',
            outcome='timed_out', failure_limit=1, release_claim=True, end_run=True,
            event_payload_extra={'budget_used': 180, 'budget_max': 180},
        )
    finally:
        if worker.poll() is None:
            worker.kill(); worker.wait(timeout=10)

    # Native dispatch promotes the tripped blocked review and claims it in the
    # same pass before its post-dispatch tick hook.  It is not a kanban_unblock
    # call, so recovery must bind at the first normal worker tool instead.
    replacement = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        resumed = b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: replacement.pid,
                                      max_spawn=1, reconcile_orphans=False)
        assert resumed.spawned == [(b.pred, 'review', str(b.repo))]
        observed = b.show()
        failed = next(run for run in observed['runs'] if run['outcome'] == 'gave_up')
        assert any(event['kind'] == 'gave_up' and event['run_id'] == failed['id']
                   and event['payload']['trigger_outcome'] == 'timed_out'
                   and event['payload']['retry_status'] == 'review'
                   and event['payload']['budget_used'] == event['payload']['budget_max'] == 180
                   for event in observed['events'])
        assert observed['task']['status'] == 'running'
        reviewer_run_id = observed['task']['current_run_id']
        assert reviewer_run_id != failed['id']
        # This direct native auto-promotion has no durable recovery lease yet:
        # claim_review_task deliberately emits no kanban_task_claimed hook, and
        # the reservation is made only by the first guarded worker tool.  Thus
        # this native-auto-promotion route has no review lease-before-admission
        # state to terminalize.
        assert state.recovery_entry(b.pred, 'default') is None

        # Native review claims have no claimed hook.  The first normal tool is
        # therefore the authoritative pre-tool admission boundary: it checks
        # the frozen implementation handoff, binds the replacement reviewer
        # run, and records native promotion rather than inventing an unblock.
        monkeypatch.setenv('HERMES_KANBAN_TASK', b.pred)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(reviewer_run_id))
        monkeypatch.setenv('HERMES_PROFILE', 'review')
        monkeypatch.setenv('HERMES_SESSION_ID', 'restarted-reviewer-' + str(reviewer_run_id))
        core_root = str(Path(hermes_cli.__file__).resolve().parent.parent)
        code = (f"import sys,json;sys.path[:0]={[str(Path(__file__).resolve().parents[1]), core_root]!r};"
                "import tools.kanban_tools;from hermes_cli.plugins import get_plugin_manager;get_plugin_manager().discover_and_load();"
                "from model_tools import handle_function_call;"
                "print(handle_function_call('submit_review', {'verdict':'approved','rationale':'Recovered reviewer used the guarded registered verdict tool.'}))")
        restarted = subprocess.run([sys.executable, '-I', '-B', '-c', code], cwd=b.repo, env=dict(os.environ),
                                  capture_output=True, text=True, timeout=45)
        assert restarted.returncode == 0, restarted.stderr
        assert not json.loads(restarted.stdout).get('error'), restarted.stdout
        entry = state.recovery_entry(b.pred, 'default')
        assert entry and entry['phase'] == 'review'
        assert entry['intent'] == {'status': 'native_resume_observed', 'native_status': 'review'}
        assert entry['authorized_run_id'] == reviewer_run_id
        assert entry['receipt']['run_id'] == reviewer_run_id
        assert entry['receipt']['tool'] == 'submit_review'
        assert len([event for event in b.show()['events'] if event['kind'] == 'unblocked']) == 1
        assert b.show()['task']['status'] == 'done'
    finally:
        if replacement.poll() is None:
            replacement.kill(); replacement.wait(timeout=10)


def test_native_spawn_failure_is_not_adopted_as_rm02(board, monkeypatch):
    """A pre-start dispatcher failure is explicitly not the RM02 recovery class."""
    b = board
    policy = state.activate_board('default', activation_id='rm02')
    state.set_recovery_policy('default', enabled=True)
    task_id = b.kb.create_task(b.conn, title='RM02 first-run failure', assignee='impl', priority=500,
                               max_retries=1, workspace_kind='dir', workspace_path=str(b.repo))
    # A production dispatcher failure closes the native run as gave_up, but no
    # worker started and it has none of run 2247's iteration-budget evidence.
    b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('controlled RM02 failure')),
                        max_spawn=1, reconcile_orphans=False)
    observed = b.show(task_id)
    assert observed['runs'][0]['outcome'] == 'gave_up'
    assert any(event['kind'] == 'gave_up' and event['payload']['trigger_outcome'] == 'spawn_failed'
               for event in observed['events'])
    assert observed['task']['status'] == 'blocked'
    assert state.task_binding(task_id, 'default') is None
    assert state.recovery_entry(task_id, 'default') is None


def test_native_rm02_started_iteration_exhaustion_is_adopted_and_admitted(board, monkeypatch):
    """Joined proof: actual started process -> native gave_up -> same-ticket recovery."""
    b = board
    policy = state.activate_board('default', activation_id='rm02-iteration-exhaustion')
    state.set_recovery_policy('default', enabled=True)
    task_id = b.kb.create_task(b.conn, title='RM02 180-turn exhausted worker', assignee='impl', priority=500,
                               max_retries=1, workspace_kind='dir', workspace_path=str(b.repo))
    (b.repo / 'partial.txt').write_text('partial implementation from exhausted worker\n')
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        spawned = b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: worker.pid,
                                      max_spawn=1, reconcile_orphans=False)
        assert spawned.spawned == [(task_id, 'impl', str(b.repo))]
        active = b.kb.get_task(b.conn, task_id)
        assert active.current_run_id is not None and worker.poll() is None
        # This is the native dispatcher terminal-failure producer used after a
        # provider reports the real iteration limit.  The process fixture has
        # actually started; the record shape mirrors run 2247, not spawn_failed.
        worker.terminate(); worker.wait(timeout=10)
        assert b.kbd._record_task_failure(
            b.conn, task_id,
            'Iteration budget exhausted (180/180) — task could not complete within the allowed iterations',
            outcome='timed_out', release_claim=True, end_run=True,
            event_payload_extra={'budget_used': 180, 'budget_max': 180},
        )
    finally:
        if worker.poll() is None:
            worker.kill(); worker.wait(timeout=10)
    failed = b.show(task_id)
    failed_run = failed['runs'][0]
    assert failed_run['outcome'] == 'gave_up'
    terminal = next(event for event in failed['events'] if event['kind'] == 'gave_up' and event['run_id'] == failed_run['id'])
    assert terminal['payload']['trigger_outcome'] == 'timed_out'
    assert terminal['payload']['budget_used'] == terminal['payload']['budget_max'] == 180
    assert any(event['kind'] == 'spawned' and event['run_id'] == failed_run['id'] for event in failed['events'])
    assert failed['task']['status'] == 'blocked'
    # A normal dispatch tick invokes the installed hook and performs supported
    # native unblock only after the exact terminal record above is durable.
    b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not respawn before recovery tick')),
                        max_spawn=1, reconcile_orphans=False)
    observed = b.show(task_id)
    assert observed['task']['status'] in {'ready', 'todo'}
    binding = state.task_binding(task_id, 'default')
    assert binding and binding['native_run_id'] == failed_run['id'] > policy['native_run_watermark']
    entry = state.recovery_entry(task_id, 'default')
    assert entry and entry['adopted'] is True and entry['checkpoint']['dirty']
    resumed = b.dispatch(expected=task_id)
    assert resumed.assignee == 'impl'
    # The registered custom implementation tool must still traverse the
    # ordinary pre-tool recovery guard.  The recovery checkpoint intentionally
    # preserves an untracked partial file, so the first guarded call pins its
    # receipt but refuses an uncommitted candidate.  A worker must commit that
    # partial change before the normal completion path can succeed.
    refused = b.call('finish_implementation', summary='Recovered implementation attempted through the guarded custom tool.')
    assert refused['error'] and 'clean' in refused['error'].lower(), refused
    entry_after_refusal = state.recovery_entry(task_id, 'default')
    assert entry_after_refusal and entry_after_refusal['receipt']
    receipt = entry_after_refusal['receipt']
    assert receipt['run_id'] == resumed.current_run_id
    assert receipt['tool'] == 'finish_implementation'
    b.git('add', 'partial.txt')
    b.git('commit', '-qm', 'recover exhausted worker partial implementation')
    assert b.call('finish_implementation', summary='Recovered implementation committed after guarded dirty refusal.')['ok']
    # The verified native handoff synchronously consumes only this implementation
    # recovery lease. The phase budget remains charged, while the normal reviewer
    # is no longer mistaken for the recovered implementer.
    stored = state.load_state()
    key = state.recovery_key('default', task_id, failed_run['id'], 'implementation')
    assert stored['recovery'][key]['terminal'] == 'native_review_handoff'
    assert state.recovery_entry(task_id, 'default') is None
    assert stored['recovery_budgets']['default:' + task_id + ':implementation'] == 1
    assert not any(entry['task_id'] == task_id and entry['phase'] == 'review' and entry.get('receipt')
                   for entry in stored['recovery'].values())
    reviewer = b.dispatch(expected=task_id)
    assert reviewer.assignee == 'review'
    approved = b.call('submit_review', verdict='approved', rationale='Normal reviewer approved the recovered implementation handoff.')
    assert approved['ok'], approved
    assert b.show(task_id)['task']['status'] == 'done'
    stored = state.load_state()
    assert stored['recovery_budgets']['default:' + task_id + ':implementation'] == 1
    assert not any(entry['task_id'] == task_id and entry['phase'] == 'review' and entry.get('receipt')
                   for entry in stored['recovery'].values())


def test_joined_configured_recovery_limit_allows_two_failed_run_identities_then_holds_after_restart(board):
    """Native fixture: two configured implementation grants survive terminalization/reload; a third does not."""
    b = board
    state.activate_board('default', activation_id='two-recovery-grants')
    state.set_recovery_policy('default', enabled=True, max_per_phase=2)
    task_id = b.kb.create_task(b.conn, title='two durable watchdog recoveries', assignee='impl', priority=500,
                               max_retries=1, workspace_kind='dir', workspace_path=str(b.repo))

    def start_current() -> subprocess.Popen:
        """Use a real started native replacement, never a synthetic successful retry."""
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            assert b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: worker.pid,
                                       max_spawn=1, reconcile_orphans=False).spawned == [(task_id, 'impl', str(b.repo))]
            active = b.kb.get_task(b.conn, task_id)
            assert active.current_run_id is not None and worker.poll() is None
            return worker
        except BaseException:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=10)
            raise

    def exhaust_current(worker: subprocess.Popen) -> int:
        worker.terminate(); worker.wait(timeout=10)
        assert b.kbd._record_task_failure(
            b.conn, task_id,
            'Iteration budget exhausted (180/180) — task could not complete within the allowed iterations',
            outcome='timed_out', release_claim=True, end_run=True,
            event_payload_extra={'budget_used': 180, 'budget_max': 180},
        )
        failed = b.show(task_id)
        failed_run = failed['runs'][-1]
        assert any(event['kind'] == 'spawned' and event['run_id'] == failed_run['id'] for event in failed['events'])
        return failed_run['id']

    first_failed = exhaust_current(start_current())
    # First hook invocation durably reserves/unblocks; the following dispatch owns the replacement.
    b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError('watchdog tick only')),
                        max_spawn=1, reconcile_orphans=False)
    second_worker = start_current()
    assert b.kb.get_task(b.conn, task_id).current_run_id != first_failed
    second_failed = exhaust_current(second_worker)

    # The first post-terminal tick only terminalizes the old authorized lease.
    # Reading the state from disk models a new watchdog process after restart.
    plugin.watchdog_tick(board='default')
    assert state.load_state()['recovery_budgets']['default:' + task_id + ':implementation'] == 1
    # A fresh tick observes the next failed-run identity and uses grant two.
    plugin.watchdog_tick(board='default')
    assert b.show(task_id)['task']['status'] in {'ready', 'todo'}
    third_worker = start_current()
    assert b.kb.get_task(b.conn, task_id).current_run_id != second_failed
    third_failed = exhaust_current(third_worker)

    # Terminalize grant two, then a restarted watchdog must hold the third failure.
    plugin.watchdog_tick(board='default')
    plugin.watchdog_tick(board='default')
    persisted = state.load_state()
    assert persisted['recovery_budgets']['default:' + task_id + ':implementation'] == 2
    assert len([entry for entry in persisted['recovery'].values()
                if entry['task_id'] == task_id and entry['phase'] == 'implementation']) == 2
    assert b.show(task_id)['task']['status'] == 'blocked'
    assert third_failed not in {entry['failed_run_id'] for entry in persisted['recovery'].values()}


def test_joined_recovery_terminal_before_receipt_releases_only_its_successor(board):
    """A claimed replacement that exits before its first receipt releases its exact lease.

    The native terminal transition, rather than a direct plugin callback, is
    deliberately followed by a normal dispatch tick.  The original exhausted
    run remains in the history, so this also proves the phase budget is not
    reset when the workspace becomes available to another task.
    """
    b = board
    state.activate_board('default', activation_id='terminal-before-admission')
    state.set_recovery_policy('default', enabled=True)
    task_id = b.kb.create_task(b.conn, title='terminal before first tool', assignee='impl', priority=500,
                               max_retries=1, workspace_kind='dir', workspace_path=str(b.repo))
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        assert b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: worker.pid,
                                   max_spawn=1, reconcile_orphans=False).spawned == [(task_id, 'impl', str(b.repo))]
        worker.terminate(); worker.wait(timeout=10)
        assert b.kbd._record_task_failure(
            b.conn, task_id,
            'Iteration budget exhausted (180/180) — task could not complete within the allowed iterations',
            outcome='timed_out', release_claim=True, end_run=True,
            event_payload_extra={'budget_used': 180, 'budget_max': 180},
        )
    finally:
        if worker.poll() is None:
            worker.kill(); worker.wait(timeout=10)

    # The normal post-dispatch tick reserves and unblocks the exact failed run.
    b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError('recovery tick only')),
                        max_spawn=1, reconcile_orphans=False)
    replacement = b.dispatch(expected=task_id)
    entry = state.recovery_entry(task_id, 'default')
    # Implementation ready claims do fire kanban_task_claimed, so normal
    # dispatch has already bound this exact successor.  It still has no
    # pre-tool receipt: native terminalization must use the authorized-run
    # branch, not misrepresent it as an unadmitted replacement.
    assert entry and entry.get('authorized_run_id') == replacement.current_run_id and entry.get('receipt') is None

    # End the exact replacement through native before it can make a model tool
    # call.  A later tick may release only this direct, phase-correct successor.
    assert b.kb.block_task(b.conn, task_id, reason='controlled pre-tool exit', kind='needs_input',
                           expected_run_id=replacement.current_run_id)
    plugin.watchdog_tick(board='default')
    key = state.recovery_key('default', task_id, entry['failed_run_id'], entry['phase'])
    stored = state.load_state()
    assert stored['recovery'][key]['terminal'] == 'native_terminal'
    assert str(b.repo) not in stored['workspace_leases']

    # Lease release is not a retry-budget reset: a different task can reserve
    # this workspace, while the original phase remains exhausted.
    other = b.kb.create_task(b.conn, title='other workspace recovery', assignee='impl', priority=1,
                              workspace_kind='dir', workspace_path=str(b.repo))
    original_binding = state.task_binding(task_id, 'default')
    assert original_binding is not None
    binding = dict(original_binding)
    binding['task_id'] = other
    state.reserve_recovery('default', other, failed_run_id=999, phase='implementation', workspace_path=str(b.repo),
                           checkpoint={'head': 'a' * 40, 'dirty': []}, binding=binding, adopted=True)
    with pytest.raises(ValueError, match='budget exhausted'):
        state.reserve_recovery('default', task_id, failed_run_id=1000, phase='implementation', workspace_path=str(b.repo),
                               checkpoint={'head': 'a' * 40, 'dirty': []}, binding=original_binding, adopted=False)


def test_unmanaged_completion_unchanged_and_readonly_snapshot(board):
    b=board
    tid=b.kb.create_task(b.conn,title='Unmanaged control',assignee='impl',priority=100,workspace_kind='dir',workspace_path=str(b.repo))
    assert b.dispatch().id==tid
    assert b.call('kanban_complete',summary='Unmanaged native completion remains unchanged.')['ok']
    before=b.show()
    raw=state.state_path().read_bytes()
    assert snapshot('default',b.pred)==before
    assert state.load_state()['tasks']
    assert state.state_path().read_bytes()==raw
    assert b.show()==before


def test_pre_run_enrollment_and_closed_model_arguments(board):
    b=board;b.dispatch()
    observed=b.show()
    with pytest.raises(ValueError,match='parked'):
        state.enroll_task(board='default',task=observed['task'],runs=observed['runs'])
    assert b.call('finish_implementation',summary='attempt',reviewer='other')['error']
    assert b.call('submit_review',verdict='approved',rationale='implementer self-approval')['error']
    b.gated()
    (b.repo/'untracked.py').write_text('uncommitted source\n')
    assert b.call('finish_implementation',summary='must reject untracked source')['error']


def test_native_dashboard_phases_configuration_and_readonly(board, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard import plugin_api
    b = board
    app = FastAPI()
    app.include_router(plugin_api.router, prefix='/api/plugins/local-first-review')
    client = TestClient(app)
    base = '/api/plugins/local-first-review'
    def view(expected, counter):
        before = b.show()
        config = state.state_path().read_bytes()
        result = client.get(base + '/status')
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['tasks'][0]['phase'] == expected, body
        assert body['counts'][counter] == 1, body
        assert b.show() == before
        assert state.state_path().read_bytes() == config
        return body
    b.dispatch()
    view('implementation', 'implementation_active')
    assert client.put(base+'/configuration', json={'implementation_profile':'impl','reviewer_profile':'other'}).status_code == 200
    assert state.task_binding(b.pred)['reviewer_profile'] == 'review'
    saved = state.state_path().read_bytes()
    for reviewer in ('impl', 'missing'):
        assert client.put(base+'/configuration', json={'implementation_profile':'impl','reviewer_profile':reviewer}).status_code == 409
        assert state.state_path().read_bytes() == saved
    assert b.call('finish_implementation', summary='Native API integration candidate, committed source and checks inspected.')['ok']
    view('awaiting_review', 'awaiting_or_under_review')
    b.dispatch()
    view('review', 'awaiting_or_under_review')
    assert b.call('submit_review', verdict='changes_requested', rationale='Add the remaining acceptance case.')['ok']
    body = view('changes_requested', 'changes_requested')
    assert body['tasks'][0]['reason'] == 'Add the remaining acceptance case.'
    b.dispatch()
    assert b.call('finish_implementation', summary='Acceptance case checked; unchanged committed candidate is ready for rereview.')['ok']
    b.dispatch()
    assert b.call('submit_review', verdict='approved', rationale='Independently inspected source and acceptance evidence; no remaining findings.')['ok']
    body = view('approved_done', 'approved_done')
    assert body['tasks'][0]['handoff_summary'] == 'Acceptance case checked; unchanged committed candidate is ready for rereview.'
    assert body['tasks'][0]['run_outcome'] == 'completed'
    assert body['tasks'][0]['error'] is None


def test_missing_profiles_and_corrupt_config_fail_closed(board):
    b=board;b.dispatch()
    (b.home/'profiles/review/config.yaml').rename(b.home/'profiles/review/removed-config.yaml')
    assert b.call('finish_implementation',summary='profile no longer available')['error']
    state.state_path().write_text('{broken')
    assert b.call('kanban_complete',summary='cannot bypass corrupt policy')['error']
    b.gated()
