"""Fail-closed runtime routing boundaries; joined native proof lives separately."""
import copy
import hashlib
import json
from types import SimpleNamespace
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
    assert state.runtime_escalation_entry('task', 'default')['intent']['transport'] == {
        'operation': 'kanban_unblock', 'result': {'status': 'ready'},
    }


def test_continuation_transport_appends_history_without_replacing_original_receipt(runtime):
    """A second permitted send is auditable but cannot overwrite first-send proof."""
    binding, entry, show = runtime
    first = {'operation': 'kanban_unblock', 'result': {'status': 'ready'}}
    second = {'operation': 'kanban_unblock', 'result': {'status': 'ready', 'continued': True}}
    state.record_runtime_escalation_transport('default', 'task', 'kanban_unblock', result=first['result'])

    state.record_runtime_escalation_transport('default', 'task', 'kanban_unblock', result=second['result'])

    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['transport'] == first
    assert stored['intent']['transport_history'] == [first, second]
    assert stored['attempts'][0]['intent']['transport'] == first


def test_state_rejects_empty_or_unbounded_operator_continuation_history(runtime):
    """The exceptional authorization is exactly one canonical receipt/snapshot pair."""
    binding, entry, show = runtime
    with pytest.raises(ValueError, match='runtime operator continuation history is invalid'):
        with state.locked_state(write=True) as data:
            data['runtime_escalations']['default:task']['intent']['operator_continuation_history'] = []


@pytest.mark.parametrize('change', ['receipt_session', 'task', 'run', 'event', 'checkpoint',
                                    'binding', 'attempt', 'transport'])
def test_operator_continuation_receipt_refuses_every_changed_reviewed_identity(monkeypatch, change):
    """Every receipt component is an authority fence, never merely audit text."""
    binding = {'board': 'default', 'task_id': 'task', 'workspace_path': '/work'}
    events = [{'id': event_id, 'kind': 'observed', 'run_id': None, 'created_at': event_id,
               'payload': {'sequence': event_id}} for event_id in range(21, 36)]
    observed = {'task': {'id': 'task', 'status': 'blocked', 'assignee': 'strong',
                         'workspace_path': '/work', 'current_run_id': None},
                'runs': [], 'events': events}
    receipt = {'scope': 'test-continuation', 'sessions': [{'id': 'reviewed'}], 'later_runs': [],
               'later_event_manifest_sha256': hashlib.sha256(json.dumps([
                   {'kind': event['kind'], 'run_id': event['run_id'], 'created_at': event['created_at'],
                    'payload': event['payload']} for event in events], sort_keys=True,
                   separators=(',', ':')).encode()).hexdigest()}
    entry = {'board': 'default', 'task_id': 'task', 'binding': binding, 'failed_run_id': 11,
             'workspace_path': '/work', 'failure': {'event_id': 20}, 'checkpoint': {'head': 'a'},
             'consumed_attempts': 1, 'attempts': [{'implementation_profile': 'strong'}],
             'intent': {'operator_no_effect_reconciliation': {}, 'transport': {
                 'operation': 'kanban_unblock', 'result': {'ok': True, 'status': 'ready', 'task_id': 't_57851039'},
             }}}
    current_binding = binding
    checkpoint = {'head': 'a'}
    supplied = copy.deepcopy(receipt)
    if change == 'receipt_session': supplied['sessions'][0]['id'] = 'changed'
    elif change == 'task': observed['task']['id'] = 'other'
    elif change == 'run': observed['runs'].append({'id': 12, 'ended_at': 1})
    elif change == 'event': observed['events'][0]['payload']['sequence'] = 'changed'
    elif change == 'checkpoint': checkpoint = {'head': 'changed'}
    elif change == 'binding': current_binding = dict(binding, task_id='other')
    elif change == 'attempt': entry['consumed_attempts'] = 2
    else: entry['intent']['transport']['result']['status'] = 'changed'
    fake_plugin = SimpleNamespace(_workspace_checkpoint=lambda _: checkpoint,
                                  _workspace_is_exclusive=lambda *_: True)
    monkeypatch.setattr(runtime_escalation, 'OPERATOR_CONTINUATION_RECEIPT', receipt)
    monkeypatch.setattr(runtime_escalation, 'lease_valid', lambda _: True)

    assert runtime_escalation._operator_continuation_refusal(
        entry, current_binding, observed, fake_plugin, supplied) is not None


@pytest.mark.parametrize('mutate_checkpoint', [False, True])
def test_continuation_rechecks_checkpoint_after_unblock_before_publishing(runtime, monkeypatch, mutate_checkpoint):
    """The exceptional unblock cannot publish a route after its workspace changes."""
    binding, entry, show = runtime
    original = {'operation': 'kanban_unblock', 'result': {
        'ok': True, 'status': 'ready', 'task_id': 't_57851039',
    }}
    show['task']['assignee'] = 'strong'
    for event_id in range(21, 36):
        show['events'].append({'id': event_id, 'kind': 'observed', 'run_id': None,
                               'created_at': event_id, 'payload': {'sequence': event_id}})
    manifest = [{'kind': event['kind'], 'run_id': event['run_id'], 'created_at': event.get('created_at'),
                 'payload': event['payload']} for event in show['events'] if event['id'] > entry['failure']['event_id']]
    receipt = {'scope': 'test-continuation', 'sessions': [], 'later_runs': [],
               'later_event_manifest_sha256': hashlib.sha256(
                   json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    proof_snapshot = copy.deepcopy(show)
    proof = {'binding': binding, 'failure': entry['failure'], 'checkpoint': entry['checkpoint'],
             'failed_run_id': entry['failed_run_id'], 'snapshot': proof_snapshot,
             'snapshot_sha256': hashlib.sha256(
                 json.dumps(proof_snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    with state.locked_state(write=True) as data:
        intent = data['runtime_escalations']['default:task']['intent']
        intent.update(status='unblock_requested', transport=original, transport_history=[original],
                      operator_no_effect_reconciliation=proof,
                      operator_continuation_history=[{'receipt': receipt, 'snapshot': copy.deepcopy(show)}])
        data['runtime_escalations']['default:task']['attempts'][0]['intent'].update(
            status='unblock_requested', transport=original)
    monkeypatch.setattr(runtime_escalation, 'OPERATOR_CONTINUATION_RECEIPT', receipt)
    changed = False

    def checkpoint(_path):
        return {'head': 'changed' * 8, 'dirty': []} if changed else entry['checkpoint']

    calls = []

    def unblock(_name, _args):
        nonlocal changed
        calls.append((_name, _args))
        changed = mutate_checkpoint
        show['task']['status'] = 'ready'
        show['events'].append({'id': 36, 'kind': 'unblocked', 'run_id': None,
                               'created_at': 36, 'payload': None})
        return {'ok': True, 'status': 'ready', 'task_id': 't_57851039'}

    monkeypatch.setattr(plugin, '_workspace_checkpoint', checkpoint)
    monkeypatch.setattr(plugin, '_dispatch', unblock)
    monkeypatch.setattr(native, 'reassign_ready_task', lambda *_: pytest.fail('continuation must not reassign'))

    assert runtime_escalation.reconcile('task', 'default') is (not mutate_checkpoint)
    assert calls == [('kanban_unblock', {'board': 'default', 'task_id': 'task'})]
    stored = state.runtime_escalation_entry('task', 'default')
    if mutate_checkpoint:
        assert state.effective_routing('task', 'default') is None
        assert stored['intent']['status'] == 'held'
        replay = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)
        assert replay['ok'] is False
        assert calls == [('kanban_unblock', {'board': 'default', 'task_id': 'task'})]
    else:
        assert state.effective_routing('task', 'default')['implementation_profile'] == 'strong'
        assert stored['intent']['status'] == 'routed'
        assert stored['intent']['transport'] == original
        assert stored['intent']['transport_history'] == [original, {
            'operation': 'kanban_unblock', 'result': {'ok': True, 'status': 'ready', 'task_id': 't_57851039'},
        }]
        replay = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)
        assert replay['ok'] is True and replay['runtime_escalation'] == stored
        assert calls == [('kanban_unblock', {'board': 'default', 'task_id': 'task'})]


def _held_operator_continuation(runtime, monkeypatch):
    """Build the exact already-spent held state; only resume_held may advance it."""
    binding, entry, show = runtime
    show['task'].update(assignee='strong', current_run_id=None)
    for event_id in range(21, 36):
        show['events'].append({'id': event_id, 'kind': 'observed', 'run_id': None,
                               'created_at': event_id, 'payload': {'sequence': event_id}})
    original_transport = {'operation': 'kanban_unblock', 'result': {
        'ok': True, 'status': 'ready', 'task_id': 't_57851039',
    }}
    manifest = [{'kind': event['kind'], 'run_id': event['run_id'],
                 'created_at': event.get('created_at'), 'payload': event['payload']}
                for event in show['events'] if event['id'] > entry['failure']['event_id']]
    receipt = {'scope': 'test-exact-operator-continuation', 'sessions': [], 'later_runs': [],
               'later_event_manifest_sha256': hashlib.sha256(
                   json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    proof_snapshot = copy.deepcopy(show)
    proof = {'binding': binding, 'failure': entry['failure'], 'checkpoint': entry['checkpoint'],
             'failed_run_id': entry['failed_run_id'], 'snapshot': proof_snapshot,
             'snapshot_sha256': hashlib.sha256(
                 json.dumps(proof_snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    with state.locked_state(write=True) as data:
        stored = data['runtime_escalations']['default:task']
        stored['intent'].update(status='held', transport=original_transport,
                                transport_history=[original_transport],
                                operator_no_effect_reconciliation=proof)
        stored['attempts'][0]['intent'].update(status='held', transport=original_transport)
    monkeypatch.setattr(runtime_escalation, 'OPERATOR_CONTINUATION_RECEIPT', receipt)
    return binding, entry, show, receipt, original_transport


def test_resume_held_exact_operator_receipt_persists_then_routes_once(runtime, monkeypatch):
    """The receipt path is a real held -> authorize -> send -> readback -> route slice."""
    binding, entry, show, receipt, original_transport = _held_operator_continuation(runtime, monkeypatch)
    authorization_snapshot = copy.deepcopy(show)
    dispatches = []

    def unblock(name, args):
        dispatches.append((name, args))
        persisted = state.runtime_escalation_entry('task', 'default')
        assert persisted['intent']['operator_continuation_history'] == [{
            'receipt': receipt, 'snapshot': copy.deepcopy(show),
        }]
        assert persisted['intent']['status'] == 'unblock_attempted'
        assert persisted['intent']['transport'] == original_transport
        show['task']['status'] = 'ready'
        show['events'].append({'id': 36, 'kind': 'unblocked', 'run_id': None,
                               'created_at': 36, 'payload': None})
        return {'ok': True, 'status': 'ready', 'task_id': 't_57851039'}

    monkeypatch.setattr(plugin, '_dispatch', unblock)
    monkeypatch.setattr(native, 'reassign_ready_task', lambda *_: pytest.fail('continuation must not reassign'))

    result = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)

    assert result['ok'] is True
    assert dispatches == [('kanban_unblock', {'board': 'default', 'task_id': 'task'})]
    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'routed'
    assert stored['intent']['transport'] == original_transport
    assert stored['intent']['transport_history'] == [original_transport, {
        'operation': 'kanban_unblock', 'result': {'ok': True, 'status': 'ready', 'task_id': 't_57851039'},
    }]
    assert stored['intent']['operator_continuation_history'] == [{'receipt': receipt,
                                                                    'snapshot': authorization_snapshot}]
    assert state.effective_routing('task', 'default')['implementation_profile'] == 'strong'
    replay = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)
    assert replay['ok'] is True
    assert dispatches == [('kanban_unblock', {'board': 'default', 'task_id': 'task'})]


@pytest.mark.parametrize('mode', ['dispatch_exception', 'readback_failure', 'duplicate_unblock'])
def test_resume_held_operator_receipt_failure_paths_hold_without_resend(runtime, monkeypatch, mode):
    """Every ambiguous continuation result consumes the one send and remains held."""
    binding, entry, show, receipt, original_transport = _held_operator_continuation(runtime, monkeypatch)
    dispatches = []
    if mode == 'readback_failure':
        original_snapshot = native.snapshot
        reads = 0

        def snapshot(*args):
            nonlocal reads
            reads += 1
            if reads == 4:
                raise RuntimeError('lost native readback')
            return original_snapshot(*args)

        monkeypatch.setattr(native, 'snapshot', snapshot)

    def unblock(*_args):
        dispatches.append('unblock')
        if mode == 'dispatch_exception':
            raise OSError('lost response after send')
        show['task']['status'] = 'ready'
        show['events'].append({'id': 36, 'kind': 'unblocked', 'run_id': None,
                               'created_at': 36, 'payload': None})
        if mode == 'duplicate_unblock':
            show['events'].append({'id': 37, 'kind': 'unblocked', 'run_id': None,
                                   'created_at': 37, 'payload': None})
        return {'ok': True, 'status': 'ready', 'task_id': 't_57851039'}

    monkeypatch.setattr(plugin, '_dispatch', unblock)
    result = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)

    assert result['ok'] is False
    assert dispatches == ['unblock']
    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'held'
    assert stored['intent']['transport'] == original_transport
    assert len(stored['intent']['operator_continuation_history']) == 1
    replay = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)
    assert replay['ok'] is False
    assert dispatches == ['unblock']


def test_resume_held_operator_receipt_lost_response_routes_from_readback(runtime, monkeypatch):
    """A send that raises after native success routes only from the unique readback receipt."""
    binding, entry, show, receipt, original_transport = _held_operator_continuation(runtime, monkeypatch)
    dispatches = []

    def lost_response(*_args):
        dispatches.append('unblock')
        show['task']['status'] = 'ready'
        show['events'].append({'id': 36, 'kind': 'unblocked', 'run_id': None,
                               'created_at': 36, 'payload': None})
        raise OSError('response lost after native success')

    monkeypatch.setattr(plugin, '_dispatch', lost_response)
    result = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)

    assert result['ok'] is True
    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'routed'
    assert stored['intent']['transport'] == original_transport
    assert stored['intent']['transport_history'][-1]['error'] == {
        'type': 'OSError', 'message': 'response lost after native success',
    }
    assert dispatches == ['unblock']


@pytest.mark.parametrize('mode', ['malformed_receipt', 'changed_manifest'])
def test_resume_held_operator_receipt_refuses_before_dispatch(runtime, monkeypatch, mode):
    """Receipt or reviewed-native-history drift is rejected before spending a new effect."""
    binding, entry, show, receipt, original_transport = _held_operator_continuation(runtime, monkeypatch)
    if mode == 'malformed_receipt':
        receipt = dict(receipt, scope='wrong-scope')
    else:
        show['events'][-1]['payload']['sequence'] = 'changed-after-review'
    dispatches = []
    monkeypatch.setattr(plugin, '_dispatch', lambda *_: dispatches.append('unblock'))

    result = runtime_escalation.resume_held('task', 'default', operator_receipt=receipt)

    assert result['ok'] is False
    assert dispatches == []
    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'held'
    assert 'operator_continuation_history' not in stored['intent']
    assert stored['intent']['transport'] == original_transport


def test_runtime_unblock_transport_error_is_persisted_before_safe_hold(runtime, monkeypatch):
    binding, entry, show = runtime

    def refused(*_args):
        raise OSError('native transport link lost after call')

    monkeypatch.setattr(plugin, '_dispatch', refused)

    assert not runtime_escalation.reconcile('task', 'default')

    stored = state.runtime_escalation_entry('task', 'default')
    transport = stored['intent']['transport']
    assert transport == {
        'operation': 'kanban_unblock',
        'error': {
            'type': 'OSError',
            'message': 'native transport link lost after call',
        },
    }
    assert stored['intent']['status'] == 'held'
    assert 'Native unblock transport failed: OSError: native transport link lost after call' in stored['intent']['reason']
    assert state.load_state()['recovery_budgets']['default:task:implementation'] == 2


def test_runtime_unblock_readback_error_is_held_after_transport_attempt(runtime, monkeypatch):
    binding, entry, show = runtime
    # Initial admission plus final pre-send authority barrier, then readback.
    snapshots = [show, show, RuntimeError('native snapshot unavailable')]

    def snapshot(*_args):
        value = snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    monkeypatch.setattr(native, 'snapshot', snapshot)
    monkeypatch.setattr(plugin, '_dispatch', lambda *_args: {'status': 'ready'})

    assert not runtime_escalation.reconcile('task', 'default')

    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'held'
    assert stored['intent']['transport'] == {
        'operation': 'kanban_unblock', 'result': {'status': 'ready'},
    }
    assert 'Native unblock readback failed: RuntimeError: native snapshot unavailable' in stored['intent']['reason']
    assert state.load_state()['recovery_budgets']['default:task:implementation'] == 2


def test_single_action_resume_refuses_later_unproven_run_without_state_change(runtime):
    """A later protocol-failed run cannot be inferred effect-free from its own summary."""
    binding, entry, show = runtime
    state.update_runtime_escalation_intent('default', 'task', 'held', reason='legacy no-effect hold')
    show['runs'].append({'id': 12, 'profile': 'impl', 'status': 'crashed', 'outcome': 'crashed',
                         'started_at': 3, 'ended_at': 4, 'error': 'protocol violation'})
    show['events'].extend([
        {'id': 21, 'kind': 'claimed', 'run_id': 12, 'payload': {'lock': 'worker-lock'}},
        {'id': 22, 'kind': 'protocol_violation', 'run_id': 12,
         'payload': {'worker_output': 'Runtime escalation routing is pending; no worker tool is authorized.'}},
    ])
    before_state = copy.deepcopy(state.load_state())
    before_native = copy.deepcopy(show)

    result = runtime_escalation.resume_held('task', 'default')

    assert result['ok'] is False
    assert 'later native run 12' in result['reason']
    assert 'the available native snapshot cannot prove it was effect-free' in result['reason']
    assert state.load_state() == before_state
    assert show == before_native


def test_explicit_held_reconciliation_proves_no_effect_then_allows_one_real_tick(runtime, monkeypatch):
    """A legacy held intent has no receipt; operator proof authorizes, not fakes, it."""
    binding, entry, show = runtime
    state.update_runtime_escalation_intent('default', 'task', 'held',
                                           reason='No unique native unblock receipt for the exact failure')
    show['runs'].append({'id': 11, 'profile': 'impl', 'status': 'gave_up', 'outcome': 'gave_up',
                         'started_at': 1, 'ended_at': 2})
    before = copy.deepcopy(show)

    assert runtime_escalation.reauthorize_held('task', 'default')

    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'unblock_requested'
    assert stored['intent']['held_reason_history'] == ['No unique native unblock receipt for the exact failure']
    evidence = stored['intent']['operator_no_effect_reconciliation']
    assert evidence['failed_run_id'] == 11
    assert evidence['snapshot'] == before
    assert 'transport' not in stored['intent']
    assert show == before
    assert state.load_state()['recovery_budgets']['default:task:implementation'] == 2

    def unblock(name, args):
        assert (name, args) == ('kanban_unblock', {'board': 'default', 'task_id': 'task'})
        show['task']['status'] = 'ready'
        show['events'].append({'id': 21, 'kind': 'unblocked', 'run_id': None, 'payload': None})
        return {'status': 'ready'}

    monkeypatch.setattr(plugin, '_dispatch', unblock)
    monkeypatch.setattr(native, 'reassign_ready_task', lambda *_: True)
    assert runtime_escalation.reconcile('task', 'default') is False
    assert state.runtime_escalation_entry('task', 'default')['intent']['status'] == 'held'


def test_held_reconciliation_is_idempotent_after_progress_without_recapturing_snapshot(runtime, monkeypatch):
    """A lost POST response returns its original proof even after the tick moved on."""
    binding, entry, show = runtime
    state.update_runtime_escalation_intent('default', 'task', 'held', reason='legacy Unknown tool no-effect hold')
    show['runs'].append({'id': 11, 'profile': 'impl', 'status': 'gave_up', 'outcome': 'gave_up',
                         'started_at': 1, 'ended_at': 2})
    assert runtime_escalation.reauthorize_held('task', 'default')
    proof = state.runtime_escalation_entry('task', 'default')['intent']['operator_no_effect_reconciliation']
    state.update_runtime_escalation_intent('default', 'task', 'held', reason='tick failed after the only attempt')
    monkeypatch.setattr(native, 'snapshot', lambda *_: pytest.fail('idempotent replay must not recapture native snapshot'))

    assert runtime_escalation.reauthorize_held('task', 'default')
    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['operator_no_effect_reconciliation'] == proof
    assert stored['intent']['status'] == 'held'
    assert stored['consumed_attempts'] == 1


def test_reauthorized_tick_revalidates_authority_immediately_before_unblock(runtime, monkeypatch):
    """A pause arriving after initial observation prevents the native send."""
    binding, entry, show = runtime
    state.update_runtime_escalation_intent('default', 'task', 'held', reason='legacy Unknown tool no-effect hold')
    show['runs'].append({'id': 11, 'profile': 'impl', 'status': 'gave_up', 'outcome': 'gave_up',
                         'started_at': 1, 'ended_at': 2})
    assert runtime_escalation.reauthorize_held('task', 'default')
    calls = []
    original_snapshot = native.snapshot
    snapshots = 0

    def snapshot(*args):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            state.set_runtime_escalation_policy('default', enabled=False)
        return original_snapshot(*args)

    monkeypatch.setattr(native, 'snapshot', snapshot)
    monkeypatch.setattr(plugin, '_dispatch', lambda *args: calls.append(args) or pytest.fail('send after pause'))

    assert not runtime_escalation.reconcile('task', 'default')
    assert calls == []
    assert state.runtime_escalation_entry('task', 'default')['intent']['status'] == 'held'


def test_runtime_policy_writer_is_busy_during_native_unblock_authority(runtime, monkeypatch):
    """A supported pause writer cannot slip between final validation and send."""
    binding, entry, show = runtime
    writer_results, effects = [], []

    def unblock(*_args):
        with pytest.raises(RuntimeError, match='runtime escalation operation is busy'):
            state.set_runtime_escalation_policy('default', enabled=False)
        writer_results.append(state.board_policy('default')['runtime_escalation']['enabled'])
        show['task']['status'] = 'ready'
        show['events'].append({'id': 21, 'kind': 'unblocked', 'run_id': None, 'payload': None})
        return {'status': 'ready'}

    def reassign(_board, _task, profile):
        effects.append(profile)
        show['task']['assignee'] = profile
        show['events'].append({'id': 22, 'kind': 'assigned', 'run_id': None,
                               'payload': {'from': 'impl', 'assignee': profile}})

    monkeypatch.setattr(plugin, '_dispatch', unblock)
    monkeypatch.setattr(native, 'reassign_ready_task', reassign)

    assert runtime_escalation.reconcile('task', 'default')
    assert writer_results == [True]
    assert effects == ['strong']


def test_reassign_final_barrier_holds_when_lease_changes_after_unblock_readback(runtime, monkeypatch):
    """A lease change after the unblock receipt must prevent reassignment/publish."""
    binding, entry, show = runtime
    original_snapshot = native.snapshot
    snapshots, reassigned = 0, []

    def snapshot(*args):
        nonlocal snapshots
        snapshots += 1
        value = original_snapshot(*args)
        if snapshots == 3:  # exact unblock readback, before the reassign barrier
            with state.locked_state(write=True) as data:
                data['workspace_leases'][binding['workspace_path']] = 'another-owner'
        return value

    def unblock(*_args):
        show['task']['status'] = 'ready'
        show['events'].append({'id': 21, 'kind': 'unblocked', 'run_id': None, 'payload': None})
        return {'status': 'ready'}

    monkeypatch.setattr(native, 'snapshot', snapshot)
    monkeypatch.setattr(plugin, '_dispatch', unblock)
    monkeypatch.setattr(native, 'reassign_ready_task', lambda *args: reassigned.append(args))

    assert not runtime_escalation.reconcile('task', 'default')
    assert snapshots == 4
    assert reassigned == []
    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'held'
    assert stored['consumed_attempts'] == 1
    assert state.load_state()['recovery_budgets']['default:task:implementation'] == 2


def test_held_reconciliation_refuses_any_later_native_effect_without_reauthorizing(runtime):
    binding, entry, show = runtime
    state.update_runtime_escalation_intent('default', 'task', 'held',
                                           reason='No unique native unblock receipt for the exact failure')
    show['events'].append({'id': 21, 'kind': 'claimed', 'run_id': 12, 'payload': {}})

    assert not runtime_escalation.reauthorize_held('task', 'default')

    stored = state.runtime_escalation_entry('task', 'default')
    assert stored['intent']['status'] == 'held'
    assert 'operator_no_effect_reconciliation' not in stored['intent']
    assert stored['consumed_attempts'] == 1
    assert state.load_state()['recovery_budgets']['default:task:implementation'] == 2


def test_held_reconciliation_refuses_a_prior_transport_outcome(runtime):
    binding, entry, show = runtime
    state.update_runtime_escalation_intent('default', 'task', 'held',
                                           reason='No unique native unblock receipt for the exact failure')
    state.record_runtime_escalation_transport('default', 'task', 'kanban_unblock', result={'status': 'unknown'})

    assert not runtime_escalation.reauthorize_held('task', 'default')
    assert state.runtime_escalation_entry('task', 'default')['intent']['transport'] == {
        'operation': 'kanban_unblock', 'result': {'status': 'unknown'},
    }


def test_runtime_transport_record_rejects_invalid_persisted_shape(runtime):
    with pytest.raises(ValueError, match='runtime escalation transport record is invalid'):
        with state.locked_state(write=True) as data:
            data['runtime_escalations']['default:task']['intent']['transport'] = {
                'operation': 'kanban_unblock', 'error': {'type': 'GateError'},
            }


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


@pytest.mark.parametrize('change',['binding','prior_escalation'])
def test_invalid_reservation_does_not_charge_attempt(runtime,change):
    binding,entry,show=runtime
    with state.locked_state(write=True) as data:
        data['runtime_escalations'].clear();data['workspace_leases'].clear()
    if change=='binding': binding=dict(binding,implementation_profile='other')
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
