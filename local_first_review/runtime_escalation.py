"""Bounded runtime escalation effect reconciliation and workspace admission."""
from contextlib import contextmanager
import fcntl
from . import state, native

@contextmanager
def exclusive_operation():
    """Serialize intent/effect/readback across processes without nesting state locks."""
    path = state.state_path().with_suffix('.runtime.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)

def hold(board, task_id, reason):
    state.update_runtime_escalation_intent(board, task_id, 'held', reason=reason + '; inspect the exact native run and workspace before operator intervention')
    return False

def lease_valid(entry):
    expected = f"{entry['board']}:{entry['task_id']}:{entry['failed_run_id']}:runtime_escalation"
    return state.load_state()['workspace_leases'].get(entry['workspace_path']) == expected

def _events(show, kind, after):
    return [e for e in show['events'] if type(e.get('id')) is int and e['id'] > after and e.get('kind') == kind]

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
        observed = native.snapshot(board, task_id)
        task = observed['task']
        if (p._runtime_failure_record(observed, entry['failed_run_id']) != entry['failure']
                or task.get('workspace_path') != entry['workspace_path']
                or p._workspace_checkpoint(entry['workspace_path']) != entry['checkpoint']
                or not p._workspace_is_exclusive(board, task_id, entry['workspace_path'])):
            return hold(board, task_id, 'Runtime failure evidence or workspace checkpoint/ownership changed')
        if task.get('status') == 'blocked':
            if status not in {'catchup_requested', 'unblock_requested'}:
                return hold(board, task_id, 'Unblock outcome is unresolved; refusing duplicate effect')
            if task.get('assignee') != binding['implementation_profile'] or observed['runs'][-1:][0:1] and observed['runs'][-1]['id'] != entry['failed_run_id']:
                return hold(board, task_id, 'Blocked task no longer matches the original failed worker')
            state.update_runtime_escalation_intent(board, task_id, 'unblock_attempted')
            try:
                p._dispatch('kanban_unblock', {'board':board, 'task_id':task_id})
            except Exception:
                pass  # The persisted unknown outcome is reconciled only through readback.
            observed = native.snapshot(board, task_id); task = observed['task']
        unblocks = _events(observed, 'unblocked', entry['failure']['event_id'])
        if len(unblocks) != 1 or task.get('status') not in {'ready','todo','running'}:
            return hold(board, task_id, 'No unique native unblock receipt for the exact failure')
        assigned = _events(observed, 'assigned', unblocks[0]['id'])
        if task.get('assignee') != target:
            if status == 'reassign_attempted' or assigned or task.get('status') == 'running':
                return hold(board, task_id, 'Old-profile claim or unresolved reassignment; refusing duplicate effect')
            if task.get('assignee') != binding['implementation_profile']:
                return hold(board, task_id, 'Native owner changed before reassignment')
            state.update_runtime_escalation_intent(board, task_id, 'reassign_attempted')
            try:
                native.reassign_ready_task(board, task_id, target)
            except Exception:
                pass
            observed = native.snapshot(board, task_id); task = observed['task']
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
