"""Fail-closed runtime routing boundaries; joined native proof lives separately."""
import copy
import pytest
from local_first_review import plugin, state, native, runtime_escalation

@pytest.mark.parametrize('mutation',['duplicate_event','duplicate_run','active_run','wrong_outcome','missing_spawn','boolean_budget'])
def test_runtime_failure_refuses_ambiguous_native_evidence(mutation):
    failed={'id':7,'outcome':'gave_up','status':'gave_up','started_at':1,'ended_at':2}
    payload={'trigger_outcome':'timed_out','effective_limit':1,'budget_used':180,'budget_max':180,'retry_status':'ready','error':'Iteration budget exhausted (180/180)'}
    event={'id':11,'kind':'gave_up','run_id':7,'payload':payload}
    show={'runs':[failed],'events':[{'id':10,'kind':'spawned','run_id':7,'payload':{'pid':42}},event]}
    assert plugin._runtime_failure_record(show,7) is not None
    if mutation=='duplicate_event': show['events'].append(copy.deepcopy(event))
    elif mutation=='duplicate_run': show['runs'].append(copy.deepcopy(failed))
    elif mutation=='active_run': failed['ended_at']=None
    elif mutation=='wrong_outcome': failed['outcome']='spawn_failed'
    elif mutation=='missing_spawn': show['events'].pop(0)
    else: payload['budget_used']=payload['budget_max']=True
    assert plugin._runtime_failure_record(show,7) is None


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'state_path', lambda: tmp_path/'state.json')
    monkeypatch.setattr(state, 'profile_exists', lambda _: True)
    monkeypatch.setattr(plugin, 'profile_exists', lambda _: True)
    monkeypatch.setattr(native, 'board_run_watermark', lambda _: 10)
    binding = {'board':'default','task_id':'task','implementation_profile':'impl','reviewer_profile':'review','workspace_path':str(tmp_path/'work')}
    state.save_state({'version':1,'implementation_profile':'impl','reviewer_profile':'review','tasks':{'default:task':binding}})
    state.activate_board('default', activation_id='a', native_run_watermark=0)
    state.set_recovery_policy('default', enabled=True, max_per_phase=2)
    state.set_runtime_escalation_policy('default', enabled=True, max_attempts=1, implementation_profile='strong', reviewer_profile='post-review')
    with state.locked_state(write=True) as data:
        data['recovery_budgets']['default:task:implementation']=2
    checkpoint={'head':'a'*40,'dirty':[]}
    failure={'run_id':11,'event_id':20,'kind':'gave_up','payload':{}}
    entry=state.reserve_runtime_escalation('default','task',failed_run_id=11,phase='implementation',binding=binding,checkpoint=checkpoint,failure=failure,workspace_path=binding['workspace_path'])
    show={'task':{'id':'task','status':'blocked','assignee':'impl','workspace_path':binding['workspace_path']},'runs':[],'events':[{'id':20,'kind':'gave_up','run_id':11,'payload':{}}]}
    monkeypatch.setattr(plugin, '_show', lambda _: copy.deepcopy(show))
    def snapshot(selected_board, selected_task):
        assert selected_board=='default' and selected_task=='task'
        return copy.deepcopy(show)
    monkeypatch.setattr(native, 'snapshot', snapshot)
    monkeypatch.setattr(plugin, '_runtime_failure_record', lambda *_: failure)
    monkeypatch.setattr(plugin, '_workspace_checkpoint', lambda _: checkpoint)
    monkeypatch.setattr(plugin, '_workspace_is_exclusive', lambda *_: True)
    return binding,entry,show

@pytest.mark.parametrize('status', ['unblock_attempted','reassign_attempted'])
def test_unknown_effect_is_not_blindly_resent(runtime, monkeypatch, status):
    binding,entry,show=runtime
    state.update_runtime_escalation_intent('default','task',status)
    if status == 'reassign_attempted':
        show['task']['status']='ready'
        show['events'].append({'id':21,'kind':'unblocked','run_id':None,'payload':None})
    effects=[]
    monkeypatch.setattr(plugin,'_dispatch',lambda *a: effects.append(a) or {})
    monkeypatch.setattr(native,'reassign_ready_task',lambda *a: effects.append(a) or False)
    plugin._reconcile_runtime_exhaustion_escalation('task','default',state.runtime_escalation_entry('task','default'))
    assert effects == []
    assert state.runtime_escalation_entry('task','default')['intent']['status']=='held'
    assert state.load_state()['recovery_budgets']['default:task:implementation']==2


def test_runtime_effect_transport_pins_selected_board(runtime,monkeypatch):
    binding,entry,show=runtime
    calls=[]
    def unblock(name,args):
        calls.append(args)
        show['task']['status']='ready'
        show['events'].append({'id':21,'kind':'unblocked','run_id':None,'payload':None})
        return {'status':'ready'}
    def reassign(board,task,profile):
        show['task']['assignee']=profile
        show['events'].append({'id':22,'kind':'assigned','run_id':None,'payload':{'from':'impl','assignee':profile}})
        return True
    monkeypatch.setattr(plugin,'_dispatch',unblock)
    monkeypatch.setattr(native,'reassign_ready_task',reassign)
    assert plugin._reconcile_runtime_exhaustion_escalation('task','default',entry)
    assert calls==[{'board':'default','task_id':'task'}]


def test_failed_stronger_attempt_is_held_without_refunding_or_recovery(runtime):
    binding,entry,show=runtime
    state.publish_runtime_escalation_routing('default','task',binding=binding)
    show['task'].update(status='blocked',assignee='strong')
    show['runs']=[{'id':12,'profile':'strong','status':'gave_up','outcome':'gave_up','ended_at':123}]
    runtime_escalation.monitor('task','default')
    stored=state.load_state()
    assert stored['runtime_escalations']['default:task']['intent']['status']=='held'
    assert stored['recovery_budgets']['default:task:implementation']==2
    assert stored['runtime_escalations']['default:task']['consumed_attempts']==1


@pytest.mark.parametrize('change',['binding','recovery_paused','prior_escalation'])
def test_invalid_reservation_does_not_charge_attempt(runtime,change):
    binding,entry,show=runtime
    with state.locked_state(write=True) as data:
        data['runtime_escalations'].clear();data['workspace_leases'].clear()
    if change=='binding': binding=dict(binding,implementation_profile='other')
    elif change=='recovery_paused': state.set_recovery_policy('default',enabled=False)
    else:
        with state.locked_state(write=True) as data:
            data['effective_routing']['default:task']={'board':'default','task_id':'task','attempt':1,'implementation_profile':'other','reviewer_profile':'post-review'}
    before=state.load_state()
    with pytest.raises(ValueError):
        state.reserve_runtime_escalation('default','task',failed_run_id=11,phase='implementation',binding=binding,
            checkpoint=entry['checkpoint'],failure=entry['failure'],workspace_path=binding['workspace_path'])
    assert state.load_state()==before


def test_runtime_trigger_reuses_configured_escalation_profiles(runtime):
    state.set_escalation_policy('default',enabled=True,normal_correction_limit=2,max_attempts=1,implementation_profile='strong',reviewer_profile='post-review')
    state.set_runtime_escalation_policy('default',enabled=False)
    configured=state.set_runtime_escalation_policy('default',enabled=True)
    assert configured['runtime_escalation']['implementation_profile']=='strong'
    assert configured['runtime_escalation']['reviewer_profile']=='post-review'
    assert configured['escalation']['normal_correction_limit']==2


def test_historical_failure_needs_explicit_catchup(runtime):
    binding,entry,show=runtime
    with state.locked_state(write=True) as data:
        data['runtime_escalations'].clear()
        data['workspace_leases'].clear()
    with pytest.raises(ValueError, match='historical'):
        state.reserve_runtime_escalation('default','task',failed_run_id=9,phase='implementation',binding=binding,
            checkpoint=entry['checkpoint'],failure=dict(entry['failure'],run_id=9),workspace_path=binding['workspace_path'])
    assert state.load_state()['runtime_escalations']=={}


def test_runtime_policy_pins_current_native_watermark(runtime):
    assert state.board_policy('default')['runtime_escalation_watermark']==10

@pytest.mark.parametrize('change', ['checkpoint','lease','profile','paused'])
def test_first_run_admission_requires_exact_checkpoint_and_exclusive_route(runtime,monkeypatch,change):
    binding,entry,show=runtime
    state.publish_runtime_escalation_routing('default','task',binding=binding)
    show['task'].update(status='running',assignee='strong',current_run_id=12)
    show['runs']=[{'id':12,'profile':'strong','status':'running','ended_at':None}]
    if change=='checkpoint': monkeypatch.setattr(plugin,'_workspace_checkpoint',lambda _: {'head':'b'*40,'dirty':[]})
    elif change=='profile': show['task']['assignee']='impl'
    elif change=='paused': state.set_runtime_escalation_policy('default',enabled=False)
    else:
        with state.locked_state(write=True) as data: data['workspace_leases'][binding['workspace_path']]='other'
    assert runtime_escalation.admit('task','default',12,'strong',show) is not None
    assert 'implementation_run_id' not in state.runtime_escalation_entry('task','default')


def test_admission_pins_one_run_then_permits_its_edits_not_a_second_attempt(runtime,monkeypatch):
    binding,entry,show=runtime
    state.publish_runtime_escalation_routing('default','task',binding=binding)
    show['task'].update(status='running',assignee='strong',current_run_id=12)
    show['runs']=[{'id':12,'profile':'strong','status':'running','ended_at':None}]
    assert runtime_escalation.admit('task','default',12,'strong',show) is None
    monkeypatch.setattr(plugin,'_workspace_checkpoint',lambda _: {'head':'b'*40,'dirty':[]})
    assert runtime_escalation.admit('task','default',12,'strong',show) is None
    show['task']['current_run_id']=13
    show['runs']=[{'id':13,'profile':'strong','status':'running','ended_at':None}]
    assert runtime_escalation.admit('task','default',13,'strong',show) is not None
    assert state.runtime_escalation_entry('task','default')['implementation_run_id']==12


@pytest.mark.parametrize('change', ['checkpoint','lease','profile','paused'])
def test_reconciliation_rejection_keeps_budget_and_holds(runtime,monkeypatch,change):
    binding,entry,show=runtime
    if change=='checkpoint': monkeypatch.setattr(plugin,'_workspace_checkpoint',lambda _: {'head':'b'*40,'dirty':[]})
    elif change=='profile': monkeypatch.setattr(plugin,'profile_exists',lambda _:False)
    elif change=='paused': state.set_runtime_escalation_policy('default',enabled=False)
    else:
        with state.locked_state(write=True) as data: data['workspace_leases'][binding['workspace_path']]='another-owner'
    monkeypatch.setattr(plugin,'_dispatch',lambda *_:pytest.fail('native effect on rejected intent'))
    assert not plugin._reconcile_runtime_exhaustion_escalation('task','default',entry)
    assert state.runtime_escalation_entry('task','default')['intent']['status']=='held'
    assert state.load_state()['recovery_budgets']['default:task:implementation']==2
