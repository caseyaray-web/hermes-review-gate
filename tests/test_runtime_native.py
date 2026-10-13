"""Installed-Hermes RM-03 catch-up: no providers, real native lifecycle."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import pytest
from test_review_gate import board
from local_first_review import plugin, state, native, runtime_escalation
from dashboard import plugin_api as api
from test_dashboard import client

def test_native_old_review_retry_cannot_manufacture_correction_threshold(board):
    b=board
    state.activate_board('default',activation_id='distinct-candidates')
    state.set_escalation_policy('default',enabled=True,normal_correction_limit=2,max_attempts=1,implementation_profile='strong',reviewer_profile='post-review')
    for cycle in range(2):
        b.dispatch(expected=b.pred)
        assert b.call('finish_implementation',summary='Same unchanged candidate deliberately retried.')['ok']
        b.dispatch(expected=b.pred)
        result=b.call('submit_review',verdict='changes_requested',rationale='Same old candidate is still missing the change.')
        assert result.get('ok'),result
        assert b.call('submit_review',verdict='changes_requested',rationale='Duplicate old verdict.')['error']
    show=b.show()
    assert plugin._changes_count(show['runs'],show['events'],state.task_binding(b.pred,'default'))==1
    assert (show['task']['status'], show['task']['block_kind'], show['task']['block_recurrences']) == ('scheduled', 'needs_input', 1)
    assert state.effective_routing(b.pred,'default') is None
    assert b.kb.get_task(b.conn,b.child).status=='todo'


@pytest.fixture
def rm03(board, monkeypatch):
    b=board
    assert b.kb.block_task(b.conn,b.pred,reason='Unrelated fixture control stays parked',kind='needs_input')
    state.activate_board('default',activation_id='rm03-existing-hold')
    state.set_recovery_policy('default',enabled=True,max_per_phase=2)
    state.set_escalation_policy('default',enabled=True,normal_correction_limit=2,max_attempts=1,implementation_profile='strong',reviewer_profile='post-review')
    from hermes_cli import kanban_db as kb
    with monkeypatch.context() as fixed_id:
        fixed_id.setattr(kb, '_new_task_id', lambda: 't_57851039')
        tid=b.kb.create_task(b.conn,title='RM-03 contract',body='Continue the existing meal planner changes; preserve downstream gates.',assignee='impl',priority=500,max_retries=1,workspace_kind='worktree',workspace_path=str(b.repo_root))
    workspace=b.workspace(tid)
    downstream=b.kb.create_task(b.conn,title='RM03 gated successor',parents=[tid],assignee='impl',workspace_kind='worktree',workspace_path=str(b.repo_root))
    assert b.dispatch(expected=tid).assignee=='impl'
    assert b.call('finish_implementation',summary='Original candidate for genuine review.')['ok']
    assert b.dispatch(expected=tid).assignee=='review'
    verdict=b.call('submit_review',verdict='changes_requested',rationale='Preserve existing servings and cover the missing API regression. Full finding set.')
    assert verdict.get('ok'),verdict
    binding=copy.deepcopy(state.task_binding(tid,'default'))
    def exhaust():
        b.clear()
        worker=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        try:
            result=b.kbd.dispatch_once(b.conn,spawn_fn=lambda *a,**k:worker.pid,max_spawn=1,reconcile_orphans=False)
            assert result.spawned==[(tid,'impl',str(workspace))],result
            worker.terminate();worker.wait(timeout=10)
            assert b.kbd._record_task_failure(b.conn,tid,'Iteration budget exhausted (180/180) — task could not complete within the allowed iterations',outcome='timed_out',release_claim=True,end_run=True,event_payload_extra={'budget_used':180,'budget_max':180})
        finally:
            if worker.poll() is None: worker.kill();worker.wait(timeout=10)
        plugin.watchdog_tick(board='default');plugin.watchdog_tick(board='default')
    for _ in range(3): exhaust()
    assert b.show(tid)['task']['status']=='blocked'
    assert state.load_state()['recovery_budgets'][f'default:{tid}:implementation']==2
    for name in ['pocketbase/pb_hooks/meal_planner.pb.js','scripts/test-meal-planner-api-core.mjs']:
        path=workspace/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('// existing incomplete RM03 work\n')
    checkpoint=plugin._workspace_checkpoint(str(workspace))
    state.set_runtime_escalation_policy('default',enabled=True,max_attempts=1,implementation_profile='strong',reviewer_profile='post-review')
    before=b.show(tid)
    # Enabling policy and polling must not sweep this already historical hold.
    plugin.watchdog_tick(board='default')
    assert state.runtime_escalation_entry(tid,'default') is None
    assert b.show(tid)==before
    b.rm_tid,b.rm_workspace,b.rm_binding,b.rm_child,b.rm_checkpoint=tid,workspace,binding,downstream,checkpoint
    return b


def adopt(b):
    before=b.show(b.rm_tid)
    result=api.runtime_escalation_catchup(api.RuntimeCatchupControl(board='default',task_id=b.rm_tid))
    assert result['runtime_escalation']['intent']['status']=='catchup_requested'
    assert b.show(b.rm_tid)==before
    replay=api.runtime_escalation_catchup(api.RuntimeCatchupControl(board='default',task_id=b.rm_tid))
    assert replay['runtime_escalation']==result['runtime_escalation']
    assert b.show(b.rm_tid)==before
    return result


@pytest.mark.parametrize('pause_recovery',[None,'before_adoption','after_adoption'])
def test_joined_rm03_explicit_catchup_preserves_dirty_work_and_fresh_review(rm03,pause_recovery):
    b=rm03;tid=b.rm_tid
    if pause_recovery=='before_adoption':
        state.set_recovery_policy('default',enabled=False)
    result=adopt(b)
    if pause_recovery=='after_adoption':
        state.set_recovery_policy('default',enabled=False)
    packet=result['runtime_escalation']['coder_context']
    assert 'Full finding set.' in str(packet['reviewer_findings'])
    assert packet['checkpoint']==b.rm_checkpoint
    b.clear()
    tick=b.kbd.dispatch_once(b.conn,spawn_fn=lambda *a,**k:pytest.fail('catchup effect tick must not spawn'),max_spawn=1,reconcile_orphans=False)
    assert not tick.spawned
    assert b.show(tid)['task']['assignee']=='strong'
    events=b.show(tid)['events'];plugin.watchdog_tick(board='default');assert b.show(tid)['events']==events
    assert state.task_binding(tid,'default')==b.rm_binding
    assert plugin._workspace_checkpoint(str(b.rm_workspace))==b.rm_checkpoint
    assert b.kb.get_task(b.conn,b.rm_child).status=='todo'
    assert b.dispatch(expected=tid).assignee=='strong'
    delivered=plugin.guard('kanban_show',{})
    assert delivered and 'Full finding set.' in delivered['message']
    assert plugin.guard('kanban_show',{}) is None
    subprocess.check_call(['git','-C',str(b.rm_workspace),'add','.'])
    subprocess.check_call(['git','-C',str(b.rm_workspace),'commit','-qm','continue existing dirty RM03 work'])
    handoff=b.call('finish_implementation',summary='Inspected and completed existing partial RM03 changes.')
    assert handoff.get('ok'),handoff
    assert b.kb.get_task(b.conn,b.rm_child).status=='todo'
    assert b.dispatch(expected=tid).assignee=='post-review'
    approved=b.call('submit_review',verdict='approved',rationale='Fresh independent verification of the continued RM03 candidate.')
    assert approved.get('ok'),approved
    assert b.show(tid)['task']['status']=='done'
    assert state.task_binding(tid,'default')==b.rm_binding
    assert state.load_state()['recovery_budgets'][f'default:{tid}:implementation']==2



def test_joined_held_unknown_transport_reconciliation_resumes_same_attempt(rm03, monkeypatch):
    """Model the old Unknown-tool no-effect hold, then recover through the public POST."""
    b = rm03
    tid = b.rm_tid
    adopted = adopt(b)
    assert 'transport' not in adopted['runtime_escalation']['intent']
    # A persisted old-version handler saw an Unknown tool response after the
    # durable reservation but did not have today's transport-receipt writer.
    # It held the original attempt without a receipt and made no native effect.
    original_dispatch = plugin._dispatch
    original_record = state.record_runtime_escalation_transport
    monkeypatch.setattr(plugin, '_dispatch', lambda *_: {'error': 'Unknown tool: kanban_unblock'})
    monkeypatch.setattr(state, 'record_runtime_escalation_transport', lambda *_args, **_kwargs: None)
    plugin.watchdog_tick(board='default')
    monkeypatch.setattr(plugin, '_dispatch', original_dispatch)
    monkeypatch.setattr(state, 'record_runtime_escalation_transport', original_record)
    held = state.runtime_escalation_entry(tid, 'default')
    assert held['intent']['status'] == 'held'
    assert 'Unknown tool' not in str(held['intent'].get('reason'))  # native proof, not response text, is authority
    assert 'transport' not in held['intent']
    before = b.show(tid)

    resumed = client().post('/runtime-escalation/resume-held', json={'board': 'default', 'task_id': tid})
    assert resumed.status_code == 200, resumed.text
    entry = resumed.json()['runtime_escalation']
    assert entry['intent']['status'] == 'routed'
    assert entry['intent']['held_reason_history'] == [held['intent']['reason']]
    assert entry['intent']['operator_no_effect_reconciliation']['snapshot'] == before
    assert 'operator_no_effect_reconciliation' in entry['intent']
    assert entry['intent']['transport']['operation'] == 'kanban_unblock'
    assert entry['consumed_attempts'] == 1
    assert state.load_state()['recovery_budgets'][f'default:{tid}:implementation'] == 2
    after_resume = b.show(tid)
    assert after_resume['task']['assignee'] == 'strong'
    assert b.kb.get_task(b.conn, b.rm_child).status == 'todo'
    before_event_ids = {e['id'] for e in before['events']}
    new_events = [e for e in after_resume['events'] if e['id'] not in before_event_ids]
    assert len([e for e in new_events if e['kind'] == 'unblocked']) == 1
    assert len([e for e in new_events if e['kind'] == 'assigned']) == 1

    # A lost HTTP response is a read-free idempotent replay, not another effect.
    with monkeypatch.context() as replay_patch:
        replay_patch.setattr(native, 'snapshot', lambda *_: pytest.fail('replay must not recapture native evidence'))
        replay_patch.setattr(plugin, '_dispatch', lambda *_: pytest.fail('replay must not unblock twice'))
        replay_patch.setattr(native, 'reassign_ready_task', lambda *_: pytest.fail('replay must not reassign twice'))
        replay = client().post('/runtime-escalation/resume-held', json={'board': 'default', 'task_id': tid})
        assert replay.status_code == 200, replay.text
        assert replay.json()['runtime_escalation'] == entry
        assert b.show(tid) == after_resume

    assert b.dispatch(expected=tid).assignee == 'strong'
    assert plugin.guard('kanban_show', {}) is not None
    subprocess.check_call(['git', '-C', str(b.rm_workspace), 'add', '.'])
    subprocess.check_call(['git', '-C', str(b.rm_workspace), 'commit', '-qm', 'resume original RM03 held attempt'])
    assert b.call('finish_implementation', summary='Completed the original held RM03 attempt.')['ok']
    assert b.dispatch(expected=tid).assignee == 'post-review'
    assert b.call('submit_review', verdict='approved', rationale='Fresh independent post-review.')['ok']
    assert b.show(tid)['task']['status'] == 'done'
    child = b.kb.get_task(b.conn, b.rm_child)
    assert child.status == 'ready'
    assert child.current_run_id is None
    assert state.task_binding(tid, 'default') == b.rm_binding
    # The operator's separate native dispatch/release remains required.
    assert b.dispatch(expected=b.rm_child).assignee == 'impl'


@pytest.mark.parametrize('lost',['unblock','reassign'])
def test_joined_lost_response_restart_never_repeats_native_effect(rm03,monkeypatch,lost):
    b=rm03;tid=b.rm_tid;adopt(b)
    calls=[]
    dispatch=plugin._dispatch;reassign=native.reassign_ready_task
    def dispatch_loss(name,args):
        result=dispatch(name,args)
        if name=='kanban_unblock':
            calls.append(name)
            if lost=='unblock': raise OSError('lost successful unblock response')
        return result
    def reassign_loss(*args):
        result=reassign(*args);calls.append('assign')
        if lost=='reassign': raise OSError('lost successful assign response')
        return result
    monkeypatch.setattr(plugin,'_dispatch',dispatch_loss)
    monkeypatch.setattr(native,'reassign_ready_task',reassign_loss)
    plugin.watchdog_tick(board='default')
    state.load_state();plugin.watchdog_tick(board='default')
    assert calls==['kanban_unblock','assign']
    assert b.show(tid)['task']['assignee']=='strong'
    assert state.runtime_escalation_entry(tid,'default')['intent']['status']=='routed'
    assert state.task_binding(tid,'default')==b.rm_binding


def test_joined_old_profile_claim_race_is_pretool_held(rm03,monkeypatch):
    b=rm03;tid=b.rm_tid;adopt(b)
    dispatch=plugin._dispatch
    def race(name,args):
        result=dispatch(name,args)
        if name=='kanban_unblock':
            assert b.dispatch(expected=tid).assignee=='impl'
        return result
    monkeypatch.setattr(plugin,'_dispatch',race)
    plugin.watchdog_tick(board='default')
    refused=plugin.guard('terminal',{})
    assert refused and refused['action']=='block'
    assert state.runtime_escalation_entry(tid,'default')['intent']['status']=='held'
    assert not any(e['kind']=='assigned' and (e['payload'] or {}).get('assignee')=='strong' for e in b.show(tid)['events'])
    assert b.kb.get_task(b.conn,b.rm_child).status=='todo'


def test_joined_operator_retry_claims_exact_latest_window_after_historical_failures(rm03, monkeypatch):
    """A disposable native retry admits only the post-unblock strong claim."""
    b = rm03
    tid = b.rm_tid
    adopt(b)
    plugin.watchdog_tick(board='default')
    assert b.dispatch(expected=tid).assignee == 'strong'
    assert b.kb.block_task(b.conn, tid, reason='bounded strong run failed', kind='needs_input',
                           expected_run_id=b.kb.get_task(b.conn, tid).current_run_id)
    b.clear()
    plugin.watchdog_tick(board='default')
    held = state.runtime_escalation_entry(tid, 'default')
    assert held['intent']['status'] == 'held'
    historical_checkpoint = held['checkpoint']
    # A prior failed coder run already consumed the one-shot context packet.
    # The explicit retry must archive this marker and create a new delivery
    # window for its exact newly admitted native run.
    with state.locked_state(write=True) as data:
        data['runtime_escalations'][f'default:{tid}']['coder_context_delivery_run_id'] = 2274
    (b.rm_workspace / 'operator-inspected-partial.py').write_text('preserve this partial retry work\n')

    dispatch = plugin._dispatch
    admitted = []
    def claim_during_unblock(name, args):
        result = dispatch(name, args)
        if name == 'kanban_unblock':
            task = b.dispatch(expected=tid)
            assert task.assignee == 'strong'
            admitted.append(task.current_run_id)
            delivered = plugin.guard('kanban_show', {})
            assert delivered and 'Fresh runtime catch-up coder context' in delivered['message']
            assert plugin.guard('kanban_show', {}) is None
            monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id + 1))
            wrong_run = plugin.guard('kanban_show', {})
            assert wrong_run and wrong_run['action'] == 'block'
            monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
        return result
    monkeypatch.setattr(plugin, '_dispatch', claim_during_unblock)

    b.clear()
    retried = runtime_escalation.operator_retry(tid, 'default', 'disposable-native-retry-1')
    assert retried['ok'], retried
    entry = state.runtime_escalation_entry(tid, 'default')
    retry = entry['intent']['operator_retry_history'][-1]
    assert retry['prior_checkpoint'] == historical_checkpoint
    assert retry['checkpoint'] == plugin._workspace_checkpoint(str(b.rm_workspace))
    assert retry['prior_coder_context_delivery_run_id'] == 2274
    assert entry['coder_context']['checkpoint'] == retry['checkpoint']
    assert entry['coder_context_delivery_run_id'] == admitted[0]
    assert retry['status'] == 'routed'
    assert admitted == [entry['implementation_run_id']]
    show = b.show(tid)
    assert runtime_escalation.admit(tid, 'default', admitted[0], 'strong', show) is None
    assert runtime_escalation.admit(tid, 'default', admitted[0] + 1, 'strong', show) is not None


def test_joined_exact_target_claim_between_unblock_and_route_publication_is_one_use(rm03, monkeypatch):
    """The reviewed held continuation admits only its one synchronous strong claim."""
    b = rm03
    tid = b.rm_tid

    # Make the exact latest native failure belong to the eventual strong owner.
    # A post-failure block creates a terminal run, so the reviewed continuation
    # must begin from this already-blocked final failure rather than manufacture
    # its status after adoption.
    assert b.kb.get_task(b.conn, tid).status == 'blocked', b.kb.get_task(b.conn, tid)
    assert b.kb.unblock_task(b.conn, tid)
    assert native.reassign_ready_task('default', tid, 'strong')
    strong_task = b.dispatch(expected=tid)
    assert strong_task.assignee == 'strong'
    assert b.kbd._record_task_failure(
        b.conn, tid,
        'Iteration budget exhausted (180/180) — task could not complete within the allowed iterations',
        outcome='timed_out', release_claim=True, end_run=True,
        event_payload_extra={'budget_used': 180, 'budget_max': 180},
    )
    assert b.kb.get_task(b.conn, tid).status == 'blocked'
    assert b.kb.get_task(b.conn, tid).assignee == 'strong'
    assert b.kb.get_task(b.conn, tid).current_run_id is None

    # This is a new target-owned failure, not the original implementation
    # catch-up.  Reserve the task-scoped runtime intent from the exact native
    # failure evidence without trying to adopt the historical RM-03 failure.
    show = b.show(tid)
    run_id = strong_task.current_run_id
    assert isinstance(run_id, int)
    failure = plugin._runtime_failure_record(show, run_id)
    assert failure is not None
    policy = state.board_policy('default')
    assert policy is not None
    watermark = policy['runtime_escalation_watermark']
    assert isinstance(watermark, int) and run_id > watermark
    assert state.recovery_budget_exhausted('default', tid, 'implementation')
    checkpoint = plugin._workspace_checkpoint(str(b.rm_workspace))
    coder_context = {
        'original_contract': dict(show['task']),
        'reviewer_findings': [
            dict(value, run_id=value['reviewer_run_id'])
            for value in plugin.changes_requested_verdicts(show['runs'], show['events'], b.rm_binding)
        ],
        'latest_failure': failure,
        'checkpoint': checkpoint,
    }
    entry = state.reserve_runtime_escalation(
        'default', tid, failed_run_id=run_id, phase='implementation',
        binding=b.rm_binding, checkpoint=checkpoint, failure=failure,
        workspace_path=str(b.rm_workspace), coder_context=coder_context, catchup=False,
    )
    assert entry['intent']['status'] == 'unblock_requested'
    assert entry['failed_run_id'] == run_id
    assert state.recovery_budget_exhausted('default', tid, 'implementation')

    # Comments are supported native operations that append the reviewed event
    # manifest without producing post-failure task runs.
    for sequence in range(15):
        assert b.kb.add_comment(b.conn, tid, 'fixture-operator',
                                f'Reviewed post-hold continuation evidence {sequence}.')

    held = b.show(tid)
    assert held['task']['status'] == 'blocked'
    assert held['task']['assignee'] == 'strong'
    assert held['task']['current_run_id'] is None
    manifest = [
        {'kind': event['kind'], 'run_id': event['run_id'], 'created_at': event.get('created_at'),
         'payload': event['payload']}
        for event in held['events'] if event['id'] > entry['failure']['event_id']
    ]
    assert len(manifest) == 15
    receipt = {
        'scope': 'fixture-exact-operator-continuation', 'sessions': [],
        'later_runs': [],
        'later_event_manifest_sha256': hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    }
    proof = {
        'binding': entry['binding'], 'failure': entry['failure'],
        'checkpoint': entry['checkpoint'], 'failed_run_id': entry['failed_run_id'],
        'snapshot': held,
        'snapshot_sha256': hashlib.sha256(
            json.dumps(held, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    }
    # Build the reviewed held continuation through state APIs only.  The
    # no-effect proof is taken from the complete native read above; no fixture
    # writes state internals or invents task identity.
    entry = state.update_runtime_escalation_intent('default', tid, 'held', reason='fixture held continuation')
    entry = state.authorize_runtime_held_reconciliation('default', tid, entry=entry, evidence=proof)
    assert entry['intent']['status'] == 'unblock_requested'
    entry = state.update_runtime_escalation_intent('default', tid, 'held', reason='fixture held continuation')
    entry = state.record_runtime_escalation_transport(
        'default', tid, 'kanban_unblock', result={'ok': True, 'status': 'ready', 'task_id': tid},
    )
    assert entry['intent']['status'] == 'held'
    assert entry['intent']['transport'] == {'operation': 'kanban_unblock', 'result': {
        'ok': True, 'status': 'ready', 'task_id': tid,
    }}
    # The reviewed one-shot continuation is intentionally scoped to this
    # fixture's canonical native task.  Keep the literal production fence
    # honest rather than replacing its receipt validator in the test.
    assert tid == 't_57851039'
    monkeypatch.setattr(runtime_escalation, 'OPERATOR_CONTINUATION_RECEIPT', receipt)

    admitted = []
    dispatch = plugin._dispatch
    def claim_after_unblock(name, args):
        # The durable effective route must exist before the blocked card becomes
        # claimable.  This assertion is intentionally inside the real native
        # transport boundary, before the actual unblock call below.
        pre_effect = state.runtime_escalation_entry(tid, 'default')
        assert pre_effect['intent']['status'] == 'routed'
        assert state.effective_routing(tid, 'default')['implementation_profile'] == 'strong'
        result = dispatch(name, args)
        if name == 'kanban_unblock':
            tick = b.kbd.dispatch_once(b.conn, spawn_fn=lambda *a, **k: os.getpid(), max_spawn=1,
                                       reconcile_orphans=False)
            assert tick.spawned == [(tid, 'strong', str(b.rm_workspace))]
            task = b.kb.get_task(b.conn, tid)
            run_id = task.current_run_id
            assert isinstance(run_id, int)
            monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
            monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run_id))
            monkeypatch.setenv('HERMES_PROFILE', 'strong')
            admitted.append(run_id)
            first = plugin.guard('kanban_show', {})
            assert first and first['message'].startswith('Fresh runtime catch-up coder context')
            assert plugin.guard('kanban_show', {}) is None
            lifecycle = plugin.guard('kanban_complete', {})
            assert lifecycle and lifecycle['action'] == 'block'
            monkeypatch.setenv('HERMES_PROFILE', 'impl')
            wrong_profile = plugin.guard('kanban_show', {})
            assert wrong_profile and wrong_profile['action'] == 'block'
            monkeypatch.setenv('HERMES_PROFILE', 'strong')
            monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run_id + 1))
            wrong_run = plugin.guard('kanban_show', {})
            assert wrong_run and wrong_run['action'] == 'block'
            monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run_id))
            current_entry = state.runtime_escalation_entry(tid, 'default')
            current_show = b.show(tid)
            fresh_unblocks = [e for e in current_show['events'] if e['kind'] == 'unblocked' and e['id'] > entry['failure']['event_id']]
            assert runtime_escalation._continuation_post_unblock_current(
                current_entry, state.task_binding(tid, 'default'), current_show, plugin, 'strong',
                fresh_unblocks[-1]['id']) is True, (current_entry, current_show, fresh_unblocks)
        return result
    monkeypatch.setattr(plugin, '_dispatch', claim_after_unblock)

    current = state.runtime_escalation_entry(tid, 'default')
    current_show = b.show(tid)
    later_runs = [{key: run.get(key) for key in ('id', 'profile', 'status', 'outcome', 'started_at', 'ended_at')}
                  for run in current_show['runs'] if run.get('id', 0) > entry['failed_run_id']]
    manifest_now = [{'kind': e['kind'], 'run_id': e['run_id'], 'created_at': e.get('created_at'), 'payload': e['payload']}
                    for e in current_show['events'] if e['id'] > entry['failure']['event_id']]
    assert later_runs == []
    assert len(manifest_now) == 15
    assert hashlib.sha256(json.dumps(manifest_now, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == receipt['later_event_manifest_sha256']
    assert runtime_escalation._operator_continuation_refusal(
        current, state.task_binding(tid, 'default'), current_show, plugin, receipt, recorded=False) is None
    # Native operator reconciliation runs outside any worker invocation context.
    b.clear()
    resumed = runtime_escalation.resume_held(tid, 'default', operator_receipt=receipt)
    assert resumed['ok'], resumed
    assert admitted and state.runtime_escalation_entry(tid, 'default')['implementation_run_id'] == admitted[0]
    assert state.runtime_escalation_entry(tid, 'default')['intent']['status'] == 'routed'
    assert state.effective_routing(tid, 'default')['implementation_profile'] == 'strong'

    show = b.show(tid)
    assert runtime_escalation.admit(tid, 'default', admitted[0] + 1, 'strong', show) is not None
    assert runtime_escalation.admit(tid, 'default', admitted[0], 'impl', show) is not None
    assert runtime_escalation.admit(tid, 'default', admitted[0], 'strong', show) is None


def test_joined_negative_post_escalation_review_holds_without_another_coder(rm03):
    b=rm03;tid=b.rm_tid;adopt(b);plugin.watchdog_tick(board='default')
    b.dispatch(expected=tid)
    assert plugin.guard('kanban_show',{}) is not None
    assert plugin.guard('kanban_show',{}) is None
    subprocess.check_call(['git','-C',str(b.rm_workspace),'add','.'])
    subprocess.check_call(['git','-C',str(b.rm_workspace),'commit','-qm','bounded candidate still needs review'])
    assert b.call('finish_implementation',summary='Fresh stronger candidate.')['ok']
    assert b.dispatch(expected=tid).assignee=='post-review'
    rejected=b.call('submit_review',verdict='changes_requested',rationale='Remaining API regression; bounded stronger attempt failed review.')
    assert rejected.get('ok'),rejected
    b.clear();plugin.watchdog_tick(board='default')
    before=b.show(tid);plugin.watchdog_tick(board='default')
    assert b.show(tid)==before
    assert before['task']['status']=='blocked'
    assert state.runtime_escalation_entry(tid,'default')['intent']['status']=='held'
    assert b.kb.get_task(b.conn,b.rm_child).status=='todo'


def test_joined_failed_terra_attempt_stays_held_without_new_budget(rm03):
    b=rm03;tid=b.rm_tid;adopt(b);plugin.watchdog_tick(board='default')
    assert b.dispatch(expected=tid).assignee=='strong'
    assert plugin.guard('kanban_show',{}) is not None # deliver frozen packet
    assert plugin.guard('kanban_show',{}) is None
    assert b.kb.block_task(b.conn,tid,reason='Terra failed bounded attempt',kind='needs_input',expected_run_id=b.kb.get_task(b.conn,tid).current_run_id)
    b.clear();plugin.watchdog_tick(board='default')
    before=b.show(tid);plugin.watchdog_tick(board='default')
    assert b.show(tid)==before and before['task']['status']=='blocked'
    assert state.runtime_escalation_entry(tid,'default')['intent']['status']=='held'
    assert state.load_state()['recovery_budgets'][f'default:{tid}:implementation']==2
    assert b.kb.get_task(b.conn,b.rm_child).status=='todo'


@pytest.mark.parametrize('change',['checkpoint','lease','native_owner','missing_profile','pause'])
def test_joined_catchup_rejections_do_not_touch_native_or_budgets(rm03,monkeypatch,change):
    b=rm03;tid=b.rm_tid;adopt(b)
    before=b.show(tid)
    if change=='checkpoint': (b.rm_workspace/'late-change.txt').write_text('changed after reservation')
    elif change=='lease':
        with state.locked_state(write=True) as data: data['workspace_leases'][str(b.rm_workspace)]='other-owner'
    elif change=='native_owner':
        competitor=b.kb.create_task(b.conn,title='Concurrent workspace owner',assignee='other',priority=1000,workspace_kind='dir',workspace_path=str(b.rm_workspace))
        assert b.dispatch(expected=competitor).assignee=='other'
        b.clear()
    elif change=='missing_profile': monkeypatch.setattr(plugin,'profile_exists',lambda _:False)
    else: state.set_runtime_escalation_policy('default',enabled=False)
    plugin.watchdog_tick(board='default');plugin.watchdog_tick(board='default')
    assert b.show(tid)==before
    entry=state.runtime_escalation_entry(tid,'default')
    assert entry['intent']['status']=='held' and entry['intent']['reason']
    assert entry['consumed_attempts']==1
    assert state.load_state()['recovery_budgets'][f'default:{tid}:implementation']==2
    assert b.kb.get_task(b.conn,b.rm_child).status=='todo'
