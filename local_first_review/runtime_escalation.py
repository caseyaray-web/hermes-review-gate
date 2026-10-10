"""Bounded runtime escalation effect reconciliation and workspace admission."""
import hashlib
import json
from typing import cast
from . import state, native

def exclusive_operation():
    """Serialize native effects with supported runtime control writers."""
    return state.runtime_escalation_operation()

def hold(board, task_id, reason):
    state.update_runtime_escalation_intent(board, task_id, 'held', reason=reason + '; inspect the exact native run and workspace before operator intervention')
    return False

def lease_valid(entry):
    expected = f"{entry['board']}:{entry['task_id']}:{entry['failed_run_id']}:runtime_escalation"
    return state.load_state()['workspace_leases'].get(entry['workspace_path']) == expected

def _events(show, kind, after):
    return [e for e in show['events'] if type(e.get('id')) is int and e['id'] > after and e.get('kind') == kind]


def _canonical_snapshot(value):
    """Return one bounded, detached complete native read or ``None``."""
    if not isinstance(value, dict) or set(value) != {'task', 'runs', 'events'}:
        return None
    if (not isinstance(value['task'], dict) or not isinstance(value['runs'], list)
            or not isinstance(value['events'], list)):
        return None
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        detached = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return (detached, encoded) if len(encoded) <= 524288 else None


def _reconciliation_proof(entry):
    proof = entry.get('intent', {}).get('operator_no_effect_reconciliation')
    if not isinstance(proof, dict):
        return None
    required = {'binding', 'failure', 'checkpoint', 'failed_run_id', 'snapshot', 'snapshot_sha256'}
    canonical = _canonical_snapshot(proof.get('snapshot'))
    if (set(proof) != required or canonical is None or not isinstance(proof.get('snapshot_sha256'), str)
            or hashlib.sha256(canonical[1]).hexdigest() != proof['snapshot_sha256']
            or proof.get('binding') != entry.get('binding') or proof.get('failure') != entry.get('failure')
            or proof.get('checkpoint') != entry.get('checkpoint')
            or proof.get('failed_run_id') != entry.get('failed_run_id')):
        return None
    return proof


def _no_effect_refusal(entry, binding, observed, p):
    """Explain why the full native snapshot cannot authorize held recovery."""
    canonical = _canonical_snapshot(observed)
    if canonical is None:
        return 'Native task/runs/events snapshot is incomplete or ambiguous.'
    show = canonical[0]
    task, runs, events = show['task'], show['runs'], show['events']
    failed_run_id, failure = entry.get('failed_run_id'), entry.get('failure')
    if type(failed_run_id) is not int or not isinstance(failure, dict) or task.get('id') != entry.get('task_id'):
        return 'Native snapshot does not identify the original failed task and run.'
    run_ids = [run.get('id') for run in runs if isinstance(run, dict)]
    event_ids = [event.get('id') for event in events if isinstance(event, dict)]
    if (len(run_ids) != len(runs) or len(event_ids) != len(events)
            or any(type(value) is not int or value <= 0 for value in [*run_ids, *event_ids])
            or len(set(run_ids)) != len(run_ids) or len(set(event_ids)) != len(event_ids)):
        return 'Native run/event history has missing, duplicate, or invalid identities.'
    run_ids = cast(list[int], run_ids)
    event_ids = cast(list[int], event_ids)
    later_runs = [run_id for run_id in run_ids if run_id > failed_run_id]
    if later_runs:
        return (f'later native run {max(later_runs)} exists after recorded failed run {failed_run_id}; '
                'the available native snapshot cannot prove it was effect-free.')
    if failed_run_id != max(run_ids, default=0):
        return 'The exact recorded failed run is absent from complete native run history.'
    if p._runtime_failure_record(show, failed_run_id) != failure:
        return 'The exact recorded failed-run failure event is missing or changed.'
    failure_event_id = failure.get('event_id')
    if type(failure_event_id) is not int or failure_event_id <= 0:
        return 'The recorded failure event has an invalid native event identity.'
    later_events = [event_id for event_id in event_ids if event_id > failure_event_id]
    if later_events:
        return f'Later native event {max(later_events)} follows the recorded failure; no-effect cannot be proven.'
    if failure_event_id != max(event_ids, default=0):
        return 'The recorded failure is not the final event in complete native event history.'
    if task.get('status') != 'blocked' or task.get('assignee') != binding.get('implementation_profile'):
        return 'Task is not still blocked under the original implementation owner.'
    if task.get('workspace_path') != entry.get('workspace_path'):
        return 'Task workspace no longer matches the immutable runtime binding.'
    if p._workspace_checkpoint(entry['workspace_path']) != entry.get('checkpoint'):
        return 'The dirty workspace checkpoint changed since the held intent was recorded.'
    if not p._workspace_is_exclusive(entry['board'], entry['task_id'], entry['workspace_path']):
        return 'The bound workspace is not exclusively owned by this task.'
    return None


def _no_effect_current(entry, binding, observed, p):
    """Verify the full current native history still equals the held proof."""
    return _no_effect_refusal(entry, binding, observed, p) is None


def _reassignment_barrier(board, task_id, p, expected_assignee):
    """Read every authority again immediately before reassigning or routing."""
    observed = native.snapshot(board, task_id)
    entry = state.runtime_escalation_entry(task_id, board)
    binding = p.task_binding(task_id, board)
    settings = (p.board_policy(board) or {}).get('runtime_escalation', {})
    if not isinstance(entry, dict) or not isinstance(binding, dict):
        return None
    attempts = entry.get('attempts')
    attempt = attempts[0] if isinstance(attempts, list) and len(attempts) == 1 and isinstance(attempts[0], dict) else None
    task = observed.get('task') if isinstance(observed, dict) else None
    if not isinstance(attempt, dict) or not isinstance(task, dict):
        return None
    target, reviewer = attempt.get('implementation_profile'), attempt.get('reviewer_profile')
    unblocks = _events(observed, 'unblocked', entry.get('failure', {}).get('event_id'))
    if (binding != entry.get('binding') or settings.get('enabled') is not True or not lease_valid(entry)
            or not isinstance(target, str) or not isinstance(reviewer, str) or target == reviewer
            or target != settings.get('implementation_profile') or reviewer != settings.get('reviewer_profile')
            or not p.profile_exists(target) or not p.profile_exists(reviewer)
            or p._runtime_failure_record(observed, entry.get('failed_run_id')) != entry.get('failure')
            or task.get('workspace_path') != entry.get('workspace_path')
            or task.get('assignee') != expected_assignee
            or task.get('status') not in {'ready', 'todo', 'running'}
            or p._workspace_checkpoint(entry['workspace_path']) != entry.get('checkpoint')
            or not p._workspace_is_exclusive(board, task_id, entry['workspace_path'])
            or len(unblocks) != 1):
        return None
    return entry, binding, target, observed, unblocks[0]


def reauthorize_held(task_id, board):
    """Prove no effect under the runtime lock without issuing a native effect."""
    with exclusive_operation() as acquired:
        if not acquired:
            return False
        authorized, _reason = _reauthorize_held_locked(task_id, board)
        return authorized


def _reauthorize_held_locked(task_id, board):
    """Return (authorized, precise refusal reason); caller owns operation lock."""
    from . import plugin as p
    entry = state.runtime_escalation_entry(task_id, board)
    if not isinstance(entry, dict):
        return False, 'No durable runtime escalation intent exists for this task.'
    if entry.get('legacy_unverifiable'):
        return False, 'The legacy runtime intent cannot be verified against its immutable binding.'
    # A client may retry after the original response was lost, including
    # after the intent progresses or re-holds. Replaying the durable proof is
    # read-free and never opens another authorization window.
    if _reconciliation_proof(entry) is not None:
        return True, None
    if entry.get('intent', {}).get('status') != 'held':
        return False, f"Runtime intent is {entry.get('intent', {}).get('status')!r}, not held; no new authorization is allowed."
    if entry.get('consumed_attempts') != 1 or 'transport' in entry.get('intent', {}):
        return False, 'Held intent does not match the legacy no-receipt, already-spent attempt state.'
    binding = p.task_binding(task_id, board)
    policy = p.board_policy(board) or {}
    settings = policy.get('runtime_escalation', {})
    attempts = entry.get('attempts')
    attempt = attempts[0] if isinstance(attempts, list) and len(attempts) == 1 and isinstance(attempts[0], dict) else {}
    target, reviewer = attempt.get('implementation_profile'), attempt.get('reviewer_profile')
    if binding != entry.get('binding'):
        return False, 'The immutable original task binding changed.'
    if settings.get('enabled') is not True:
        return False, 'Runtime escalation is disabled or paused.'
    if (target == reviewer or target != settings.get('implementation_profile')
            or reviewer != settings.get('reviewer_profile')
            or not p.profile_exists(target or '') or not p.profile_exists(reviewer or '')):
        return False, 'Configured implementation/reviewer route changed or a required profile is unavailable.'
    if not lease_valid(entry):
        return False, 'The exclusive runtime workspace lease is absent or changed.'
    try:
        canonical = _canonical_snapshot(native.snapshot(board, task_id))
    except Exception as exc:
        return False, f'Complete native task/runs/events snapshot is unavailable: {type(exc).__name__}: {exc}'
    if canonical is None:
        return False, 'Native task/runs/events snapshot is incomplete or ambiguous.'
    observed, encoded = canonical
    refusal = _no_effect_refusal(entry, binding, observed, p)
    if refusal:
        return False, refusal
    failure = entry['failure']
    evidence = {
        'binding': binding,
        'failure': failure,
        'checkpoint': entry['checkpoint'],
        'failed_run_id': entry['failed_run_id'],
        'snapshot': observed,
        'snapshot_sha256': hashlib.sha256(encoded).hexdigest(),
    }
    state.authorize_runtime_held_reconciliation(board, task_id, entry=entry, evidence=evidence)
    return True, None


def resume_held(task_id, board):
    """One operator action: prove, authorize, then reconcile once under effect fences."""
    with exclusive_operation() as acquired:
        if not acquired:
            return {'ok': False, 'reason': 'Runtime routing is busy; no recovery state was changed.'}
        entry = state.runtime_escalation_entry(task_id, board)
        if not isinstance(entry, dict):
            return {'ok': False, 'reason': 'No durable runtime escalation intent exists for this task.'}
        status = entry.get('intent', {}).get('status')
        proof = _reconciliation_proof(entry)
        raw_proof = entry.get('intent', {}).get('operator_no_effect_reconciliation')
        if raw_proof is not None and proof is None:
            return {'ok': False, 'reason': 'Stored no-effect evidence is malformed or conflicts with the original intent.'}
        if status == 'routed' and proof is not None:
            return {'ok': True, 'runtime_escalation': entry,
                    'message': 'This exact held intent is already routed; no native effect was repeated.'}
        if status == 'held':
            if proof is not None:
                return {'ok': False, 'reason': 'This authorized continuation is already held after its one effect window; refusing another attempt.'}
            authorized, reason = _reauthorize_held_locked(task_id, board)
            if not authorized:
                return {'ok': False, 'reason': reason or 'Held-intent proof failed; native state was not changed.'}
            entry = state.runtime_escalation_entry(task_id, board)
            proof = _reconciliation_proof(entry) if isinstance(entry, dict) else None
            status = entry.get('intent', {}).get('status') if isinstance(entry, dict) else None
        elif status != 'unblock_requested' or proof is None:
            return {'ok': False, 'reason': f'Runtime intent is {status!r}; only an unattempted, explicitly authorized held continuation can resume.'}
        if status != 'unblock_requested' or proof is None:
            return {'ok': False, 'reason': 'No durable one-shot authorization is available for native reconciliation.'}

    # Reconciliation reacquires the same operation lock and re-reads every
    # mutable authority input before effects. A race in this gap fails closed.
    if not reconcile(task_id, board):
        entry = state.runtime_escalation_entry(task_id, board)
        if isinstance(entry, dict) and entry.get('intent', {}).get('status') == 'routed':
            return {'ok': True, 'runtime_escalation': entry,
                    'message': 'The exact intent was routed by a concurrent serialized reconciliation; no effect was repeated.'}
        reason = entry.get('intent', {}).get('reason') if isinstance(entry, dict) else None
        return {'ok': False, 'reason': reason or 'Native reconciliation did not complete; the intent remains held.'}
    entry = state.runtime_escalation_entry(task_id, board)
    if not isinstance(entry, dict) or entry.get('intent', {}).get('status') != 'routed':
        return {'ok': False, 'reason': 'Native effects lacked exact route readback; the intent remains held.'}
    return {'ok': True, 'runtime_escalation': entry,
            'message': 'The same held intent was reconciled and routed; the normal native dispatcher may admit its single authorized run.'}


def reconcile(task_id, board):
    from . import plugin as p
    with exclusive_operation() as acquired:
        if not acquired:
            return False
        entry = state.runtime_escalation_entry(task_id, board)
        if not entry:
            return False
        status = entry['intent']['status']
        if status in {'routed', 'held'}:
            return status == 'routed'
        binding = p.task_binding(task_id, board)
        policy = p.board_policy(board) or {}
        settings = policy.get('runtime_escalation', {})
        attempt = entry['attempts'][0]
        target, reviewer = attempt['implementation_profile'], attempt['reviewer_profile']
        if (entry.get('legacy_unverifiable') or binding != entry['binding']
                or settings.get('enabled') is not True
                or target == reviewer or not p.profile_exists(target) or not p.profile_exists(reviewer)
                or target != settings.get('implementation_profile') or reviewer != settings.get('reviewer_profile')
                or not lease_valid(entry)):
            return hold(board, task_id, 'Runtime policy, profiles, original binding or exclusive lease changed')
        try:
            observed = native.snapshot(board, task_id)
        except Exception as exc:
            return hold(board, task_id, f'Native reconciliation read failed: {type(exc).__name__}: {exc}')
        task = observed['task']
        if (p._runtime_failure_record(observed, entry['failed_run_id']) != entry['failure']
                or task.get('workspace_path') != entry['workspace_path']
                or p._workspace_checkpoint(entry['workspace_path']) != entry['checkpoint']
                or not p._workspace_is_exclusive(board, task_id, entry['workspace_path'])):
            return hold(board, task_id, 'Runtime failure evidence or workspace checkpoint/ownership changed')
        transport_error = None
        if task.get('status') == 'blocked':
            if status not in {'catchup_requested', 'unblock_requested'}:
                return hold(board, task_id, 'Unblock outcome is unresolved; refusing duplicate effect')
            if task.get('assignee') != binding['implementation_profile'] or observed['runs'][-1:][0:1] and observed['runs'][-1]['id'] != entry['failed_run_id']:
                return hold(board, task_id, 'Blocked task no longer matches the original failed worker')
            # The previous observation is stale at the native-effect boundary.
            # Revalidate every mutable authority input before spending the one
            # already-reserved native send.
            try:
                entry = state.runtime_escalation_entry(task_id, board)
                binding = p.task_binding(task_id, board)
                observed = native.snapshot(board, task_id)
                # Read policy after the last native read: a control write can
                # race that read while the operation lock is held locally.
                policy = p.board_policy(board) or {}
                settings = policy.get('runtime_escalation', {})
                proof = _reconciliation_proof(entry) if isinstance(entry, dict) else None
                final_attempts = entry.get('attempts') if isinstance(entry, dict) else None
                final_attempt = final_attempts[0] if isinstance(final_attempts, list) and len(final_attempts) == 1 and isinstance(final_attempts[0], dict) else None
                final_target = final_attempt.get('implementation_profile') if isinstance(final_attempt, dict) else None
                final_reviewer = final_attempt.get('reviewer_profile') if isinstance(final_attempt, dict) else None
                task = observed.get('task') if isinstance(observed, dict) else None
                ordinary_current = (isinstance(task, dict)
                                    and p._runtime_failure_record(observed, entry['failed_run_id']) == entry['failure']
                                    and task.get('status') == 'blocked'
                                    and task.get('assignee') == binding.get('implementation_profile')
                                    and task.get('workspace_path') == entry.get('workspace_path')
                                    and p._workspace_checkpoint(entry['workspace_path']) == entry.get('checkpoint')
                                    and p._workspace_is_exclusive(board, task_id, entry['workspace_path']))
                if (not isinstance(entry, dict) or binding != entry.get('binding')
                        or settings.get('enabled') is not True or not lease_valid(entry)
                        or not isinstance(final_target, str) or not isinstance(final_reviewer, str)
                        or final_target == final_reviewer
                        or final_target != settings.get('implementation_profile')
                        or final_reviewer != settings.get('reviewer_profile')
                        or not p.profile_exists(final_target) or not p.profile_exists(final_reviewer)
                        or (proof is None and (entry.get('intent', {}).get('operator_no_effect_reconciliation') is not None
                                               or not ordinary_current))
                        or (proof is not None and (not _no_effect_current(entry, binding, observed, p)
                                                   or observed != proof['snapshot']))):
                    return hold(board, task_id, 'Runtime authority or no-effect proof changed before native unblock')
            except Exception as exc:
                return hold(board, task_id, f'Runtime authority revalidation failed before native unblock: {type(exc).__name__}: {exc}')
            state.update_runtime_escalation_intent(board, task_id, 'unblock_attempted')
            try:
                result = p._dispatch('kanban_unblock', {'board': board, 'task_id': task_id})
            except Exception as exc:
                state.record_runtime_escalation_transport(board, task_id, 'kanban_unblock', error=exc)
                transport_error = f'{type(exc).__name__}: {exc}'
            else:
                state.record_runtime_escalation_transport(board, task_id, 'kanban_unblock', result=result)
            try:
                observed = native.snapshot(board, task_id)
            except Exception as exc:
                return hold(board, task_id, f'Native unblock readback failed: {type(exc).__name__}: {exc}')
            task = observed['task']
        unblocks = _events(observed, 'unblocked', entry['failure']['event_id'])
        if len(unblocks) != 1 or task.get('status') not in {'ready','todo','running'}:
            if transport_error is not None:
                return hold(board, task_id, f'Native unblock transport failed: {transport_error}')
            return hold(board, task_id, 'No unique native unblock receipt for the exact failure')
        # The unblock readback is not authority to assign: controls, lease,
        # binding, profiles, checkpoint and native ownership are all mutable.
        try:
            expected_owner = target if status == 'reassign_attempted' else binding['implementation_profile']
            barrier = _reassignment_barrier(board, task_id, p, expected_owner)
        except Exception as exc:
            return hold(board, task_id, f'Runtime authority revalidation failed before native reassignment: {type(exc).__name__}: {exc}')
        if barrier is None:
            return hold(board, task_id, 'Runtime authority changed before native reassignment')
        entry, binding, target, observed, unblock = barrier
        unblocks = [unblock]
        task = observed['task']
        assigned = _events(observed, 'assigned', unblocks[0]['id'])
        if task.get('assignee') != target:
            if status == 'reassign_attempted' or assigned or task.get('status') == 'running':
                return hold(board, task_id, 'Old-profile claim or unresolved reassignment; refusing duplicate effect')
            if task.get('assignee') != binding['implementation_profile']:
                return hold(board, task_id, 'Native owner changed before reassignment')
            state.update_runtime_escalation_intent(board, task_id, 'reassign_attempted')
            reassign_error = None
            try:
                native.reassign_ready_task(board, task_id, target)
            except Exception as exc:
                reassign_error = f'{type(exc).__name__}: {exc}'
            try:
                observed = native.snapshot(board, task_id)
            except Exception as exc:
                return hold(board, task_id, f'Native reassignment readback failed: {type(exc).__name__}: {exc}')
            task = observed['task']
            assigned = _events(observed, 'assigned', unblocks[0]['id'])
            if reassign_error is not None and not assigned:
                return hold(board, task_id, f'Native reassignment transport failed: {reassign_error}')
        # Recheck once more before publishing a route after the native effect.
        try:
            barrier = _reassignment_barrier(board, task_id, p, target)
        except Exception as exc:
            return hold(board, task_id, f'Runtime authority revalidation failed before route publication: {type(exc).__name__}: {exc}')
        if barrier is None:
            return hold(board, task_id, 'Runtime authority changed before route publication')
        entry, binding, target, observed, unblock = barrier
        unblocks = [unblock]
        task = observed['task']
        assigned = _events(observed, 'assigned', unblocks[0]['id'])
        if (len(assigned) != 1 or assigned[0].get('payload') != {'assignee':target,'from':binding['implementation_profile']}
                or task.get('assignee') != target or task.get('status') not in {'ready','todo','running'}):
            return hold(board, task_id, 'No unique native reassignment receipt for configured escalation')
        if task.get('status') == 'running':
            runs=[r for r in observed['runs'] if r.get('id')==task.get('current_run_id')]
            claims=[e for e in _events(observed,'claimed',assigned[0]['id']) if e.get('run_id')==task.get('current_run_id')]
            if len(runs)!=1 or len(claims)!=1 or runs[0].get('profile')!=target or runs[0].get('ended_at') is not None:
                return hold(board,task_id,'Ambiguous claim raced native reassignment')
        state.publish_runtime_escalation_routing(board, task_id, binding=binding)
        return True


def monitor(task_id, board):
    """A terminal stronger worker spends the single attempt; never recover it."""
    from . import plugin as p
    with exclusive_operation() as acquired:
        if not acquired:
            return
        entry=state.runtime_escalation_entry(task_id,board)
        if not entry or entry['intent']['status']!='routed':
            return
        show=native.snapshot(board, task_id)
        later=[r for r in show['runs'] if type(r.get('id')) is int and r['id']>entry['failed_run_id']]
        failures=[r for r in later if r.get('ended_at') is not None and r.get('outcome') not in {'review_requested','completed'}]
        if failures or show['task'].get('status') in {'blocked','triage','failed'}:
            hold(board,task_id,'Bounded stronger implementation/review attempt failed; no further automatic attempt is permitted')


def admit(task_id, board, run_id, profile, show):
    """Pin exactly one coder to the reserved checkpoint before its first tool."""
    from . import plugin as p
    def reject(reason):
        return {'action':'block','message':'Runtime escalation admission refused: '+reason}
    with exclusive_operation() as acquired:
        if not acquired:
            return reject('workspace routing operation is busy; retry after reconciliation')
        entry = state.runtime_escalation_entry(task_id, board)
        policy = state.board_policy(board) or {}
        settings = policy.get('runtime_escalation', {})
        if not entry or entry['intent']['status'] != 'routed':
            return reject('route is not published')
        if settings.get('enabled') is not True or not lease_valid(entry):
            return reject('policy is paused or exclusive workspace lease changed')
        route=entry['attempts'][0]
        task=show['task']
        if (task.get('workspace_path')!=entry['workspace_path'] or task.get('current_run_id')!=run_id
                or task.get('assignee')!=profile or task.get('status')!='running'
                or not p._workspace_is_exclusive(board,task_id,entry['workspace_path'])
                or not p.profile_exists(route['implementation_profile']) or not p.profile_exists(route['reviewer_profile'])):
            return reject('native run/workspace ownership does not match the reserved route')
        active=[r for r in show['runs'] if r.get('id')==run_id and r.get('profile')==profile and r.get('ended_at') is None]
        if len(active)!=1 or type(run_id) is not int or run_id<=entry['failed_run_id']:
            return reject('not an exact fresh native run')
        if profile==route['reviewer_profile']:
            if not entry.get('implementation_run_id'):
                return reject('no admitted implementation exists for this review')
            claims=[e for e in show['events'] if e.get('kind')=='claimed' and e.get('run_id')==run_id]
            if len(claims)!=1 or (claims[0].get('payload') or {}).get('source_status')!='review':
                return reject('independent reviewer lacks an exact review claim')
            return None
        if profile!=route['implementation_profile']:
            return reject('wrong implementation profile')
        pinned=entry.get('implementation_run_id')
        if pinned is not None:
            return None if pinned==run_id else reject('bounded implementation attempt already spent')
        if p._workspace_checkpoint(entry['workspace_path'])!=entry['checkpoint']:
            hold(board,task_id,'Workspace checkpoint changed before first coder tool')
            return reject('workspace checkpoint changed before first coder tool')
        with state.locked_state(write=True) as data:
            current=data['runtime_escalations'][state.binding_key(board,task_id)]
            if current!=entry:
                return reject('runtime intent changed during admission')
            current['implementation_run_id']=run_id
        return None
