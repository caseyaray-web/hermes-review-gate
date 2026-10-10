"""Installed-Hermes RM-03 catch-up: no providers, real native lifecycle."""
import copy
import os
import subprocess
import sys
import pytest
from test_review_gate import board
from local_first_review import plugin, state, native, runtime_escalation
from dashboard import plugin_api as api

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
    assert show['task']['status'] in {'blocked','triage'}
    assert state.effective_routing(b.pred,'default') is None
    assert b.kb.get_task(b.conn,b.child).status=='todo'


@pytest.fixture
def rm03(board):
    b=board
    assert b.kb.block_task(b.conn,b.pred,reason='Unrelated fixture control stays parked',kind='needs_input')
    state.activate_board('default',activation_id='rm03-existing-hold')
    state.set_recovery_policy('default',enabled=True,max_per_phase=2)
    state.set_escalation_policy('default',enabled=True,normal_correction_limit=2,max_attempts=1,implementation_profile='strong',reviewer_profile='post-review')
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



def test_joined_held_unknown_transport_reconciliation_resumes_same_attempt(rm03):
    """Old held RM03 has no receipt; explicit proof resumes its original attempt."""
    b = rm03
    tid = b.rm_tid
    adopted = adopt(b)
    assert 'transport' not in adopted['runtime_escalation']['intent']
    state.update_runtime_escalation_intent('default', tid, 'held',
                                           reason='No unique native unblock receipt for the exact failure')
    before = b.show(tid)

    reconciled = api.runtime_escalation_reconcile_held(api.RuntimeCatchupControl(board='default', task_id=tid))

    entry = reconciled['runtime_escalation']
    assert entry['intent']['status'] == 'unblock_requested'
    assert entry['intent']['held_reason_history'] == ['No unique native unblock receipt for the exact failure']
    assert entry['intent']['operator_no_effect_reconciliation']['snapshot'] == before
    assert 'transport' not in entry['intent']
    assert b.show(tid) == before
    assert entry['consumed_attempts'] == 1
    assert state.load_state()['recovery_budgets'][f'default:{tid}:implementation'] == 2

    plugin.watchdog_tick(board='default')
    assert b.dispatch(expected=tid).assignee == 'strong'
    assert plugin.guard('kanban_show', {}) is not None
    subprocess.check_call(['git', '-C', str(b.rm_workspace), 'add', '.'])
    subprocess.check_call(['git', '-C', str(b.rm_workspace), 'commit', '-qm', 'resume original RM03 held attempt'])
    assert b.call('finish_implementation', summary='Completed the original held RM03 attempt.')['ok']
    assert b.dispatch(expected=tid).assignee == 'post-review'
    assert b.call('submit_review', verdict='approved', rationale='Fresh independent post-review.')['ok']
    assert b.show(tid)['task']['status'] == 'done'


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
