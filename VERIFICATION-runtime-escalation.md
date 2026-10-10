# Runtime-exhaustion escalation verification

## Candidate and boundaries

- Base: `b66dfe5ff748ce2214a0ff003496b4957ba92a77`.
- Verified implementation: `3d76e3452210028a8971abebb7fa944ee6fbc559`.
- Branch: `feat/runtime-exhaustion-escalation`.
- Isolated worktree: `/home/ocadmin/Documents/hermes-review-gate-runtime-escalation`.
- No installation, deployment, live board/policy/profile mutation or gateway restart. Live RM-03 was not resumed. Its dirty worktree was not modified or committed.
- Original repository remains clean at the base SHA; installed Hermes source checkout is clean.
- Tests use installed Hermes native code with disposable homes/boards/worktrees and controlled worker spawning, not provider-backed Terra execution. Native mutation guards remain enabled for delegated children. Parent evidence below supersedes earlier child skipped-test reports.

## RED → GREEN evidence

Observed failing regressions before implementation included unknown-effect duplicate dispatch, historical watermark, first-tool run/checkpoint admission, ambiguous terminal events, failed stronger-attempt holds, same-candidate review retry, automatic coder context and board-pinned effect transport.

The final independent SPEC review identified recovery-policy coupling. Added native `before_adoption` and `after_adoption` recovery-pause variants before fixing production: **2 failed, 1 passed** (`runtime-escalation-parent-bir1hw4a`). Both variants then passed in the final full suite. Removed the obsolete unit expectation that a paused recovery policy must reject a separately enabled, exhausted-budget runtime continuation.

## Final commands and results

Run from the isolated worktree. The runner is reproduced below because scratch artifacts are temporary.

```sh
uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx --with psutil --with requests python /home/ocadmin/.hermes/cache/scratch/run_runtime_escalation_tests.py tests/test_runtime_native.py tests/test_runtime_safety.py tests/test_review_evidence.py
# 44 passed in 12.54s; no skips
# /home/ocadmin/.hermes/cache/scratch/runtime-escalation-parent-s46fnkcq/results.xml

uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx --with psutil --with requests python /home/ocadmin/.hermes/cache/scratch/run_runtime_escalation_tests.py
# 159 passed, 1 warning in 29.30s; zero skipped/failed
# /home/ocadmin/.hermes/cache/scratch/runtime-escalation-parent-w5boe1sd/results.xml
# Parsed XML: 38 tests in native lifecycle modules, including 14 runtime-native cases.
# Warning: external Starlette FastAPI TestClient/httpx deprecation.

uv run --no-project --with build python -m build --outdir /home/ocadmin/.hermes/cache/scratch/runtime-escalation-build-3d76e34
# Successfully built local_first_review-1.0.0.tar.gz and local_first_review-1.0.0-py3-none-any.whl

node --check dashboard/dist/index.js
# exit 0

git diff --check
# exit 0
```

Plugin Doctor was run with outbound network blocked by Doctor and an isolated disposable home, never the installed plugin:

```sh
uv run --no-project --with ruamel.yaml --with pydantic --with psutil --with requests python -c "import os,sys,tempfile,pathlib; p=pathlib.Path(tempfile.mkdtemp(prefix='runtime-doctor-',dir='/home/ocadmin/.hermes/cache/scratch')); [os.environ.pop(k) for k in list(os.environ) if k.startswith(('HERMES_KANBAN_','HERMES_PROFILE','HERMES_SESSION'))]; os.environ.update(HOME=str(p/'home'),HERMES_HOME=str(p/'hermes'),HERMES_KANBAN_HOME=str(p/'hermes'),HERMES_DISABLE_LAZY_INSTALLS='1');sys.path.insert(0,'/home/ocadmin/.hermes/hermes-agent');from hermes_cli.plugin_dev import doctor_plugin;r=doctor_plugin('.');print(r.format_text());raise SystemExit(0 if r.ok else 1)"
# OK: runtime discovery, manifest parsing, import, and registration passed
# registrations: 2 tool(s), 3 hook(s)
```

Built wheel SHA-256: `ebf30f8770aeaef31b70cbfb2160383572e254deed38ba068ed45558ef31385b`. All six packaged runtime Python modules were byte-compared with the candidate source and matched. The wheel was built, not installed.

### Isolation runner used

```python
import os
import pathlib
import sys
import tempfile
p = pathlib.Path(tempfile.mkdtemp(prefix='runtime-escalation-parent-', dir='/home/ocadmin/.hermes/cache/scratch'))
for k in list(os.environ):
    if k.startswith(('HERMES_KANBAN_', 'HERMES_PROFILE', 'HERMES_SESSION')):
        os.environ.pop(k)
os.environ.update(HOME=str(p/'os-home'), HERMES_HOME=str(p/'hermes'), HERMES_KANBAN_HOME=str(p/'hermes'), HERMES_DISABLE_LAZY_INSTALLS='1')
sys.path.insert(0, '/home/ocadmin/.hermes/hermes-agent')
sys.path.insert(0, '/home/ocadmin/Documents/hermes-review-gate-runtime-escalation')
import pytest
print('ISOLATED_ROOT='+str(p), flush=True)
raise SystemExit(pytest.main([*(sys.argv[1:] or ['tests']), '-q', '-o', 'addopts=', '--tb=short', '--basetemp='+str(p/'pytest'), '--junitxml='+str(p/'results.xml')]))
```

## Acceptance and routing-consumer audit

| Boundary | Evidence |
|---|---|
| Exact started native iteration-exhaustion failure and separate exhausted recovery budget | `_runtime_failure_record`, reservation ledger; malformed-event safety tests; three real disposable failed runs and two recovery grants |
| Historical policy changes do not sweep holds | Native enable-time watermark; RM-03 fixture asserts unchanged blocked snapshot after policy enable/tick |
| Explicit catch-up and same card/worktree | `runtime_escalation_catchup`; native next-dispatch-tick test asserts immutable binding, dirty checkpoint, original ID and workspace unchanged |
| Durable effect intent and no duplicate effects | `runtime_escalation.reconcile`; lost unblock/reassign responses and duplicate-tick native cases; unknown-effect unit holds |
| Recovery consumer | Watchdog reconciles runtime intents separately and skips ordinary recovery for tasks with runtime attempts; failed stronger worker remains held without budget refunds |
| Implementation admission | Existing binding admission plus effective route; `runtime_escalation.admit` pins exactly one fresh run/checkpoint/lease before tools; native old-profile race is blocked |
| Handoff consumer | `finish_implementation` uses effective profiles and frozen original binding; fresh candidate handoff required, dirty work cannot masquerade as accepted |
| Review dispatch/admission | Native same-card review handoff selects independent post-escalation profile; reviewer claim and distinct terminal session required |
| Review counting | Exact native handoff/claim/verdict evidence; distinct-candidate correction counting; native repeated old review cannot manufacture threshold |
| Dashboard/status | Original binding and effective route remain separate; runtime intent/held reason exposed; approval uses exact independent native review evidence |
| Continuation context | Original contract, complete genuine findings, latest failed-run record and dirty checkpoint; coder-only packet, not reviewer packet |
| Downstream gate and bounded failure | Native child stays gated through implementation and fresh review; negative post-review and failed Terra attempt hold without another coder |
| Rejections | Native checkpoint drift, concurrent native owner, lease loss, missing profile and runtime pause cases; malformed evidence and wrong/fresh-run tests; spent budget unchanged |

## Independent review

- STANDARDS axis: GO at `59c95de36dacb857026d40ec2afb1ee988a1ec3d`; no concrete rule/safety defect found.
- SPEC axis: GO on correction re-review at `3d76e3452210028a8971abebb7fa944ee6fbc559`. Reviewer verified the removed recovery-policy coupling and retained evidence/budget/lease gates; parent JUnit reports 159 tests with zero failures/errors/skips. Delegated review runs intentionally skip native mutation cases. No remaining implementation blocker was reported.

## Held-intent operator recovery (RM-03)

A historical held intent with the reason `No unique native unblock receipt for the exact failure` has **no transport receipt**. Do not add one or modify native state manually. After the explicit catch-up intent exists, the operator may call the task-scoped `POST /runtime-escalation/reconcile-held` control for that same board/task.

Under the runtime exclusive-operation lock, this control reads one complete native snapshot and refuses unless the immutable binding, enabled distinct configured profiles, exclusive lease, task workspace/owner, dirty checkpoint, exact latest terminal iteration-exhaustion run, and full event/run history still prove that no post-failure native effect occurred. It persists the complete snapshot, canonical SHA-256 digest, previous held reason history, and the original intent binding as no-effect reconciliation evidence. It does **not** create a transport receipt, refund/re-reserve the consumed attempt, or call native mutation transport. One later real dispatcher tick performs the already-reserved native unblock/reassignment; any failure re-holds the same intent.

The joined parent-owned regression is `tests/test_runtime_native.py::test_joined_held_unknown_transport_reconciliation_resumes_same_attempt`; it covers no-receipt hold → explicit proof → real dispatch → strong implementation admission → independent post-review. Child contexts retain their native mutation guard and intentionally skip this parent-owned proof.

## RM-03 control and deployment boundary

README documents the exact task-scoped controls for `default / t_57851039`: enable the separate runtime trigger using configured escalation profiles, then explicitly adopt that task through `/runtime-escalation/catch-up`. For an already-held adopted intent, use `/runtime-escalation/reconcile-held` only after it can prove the no-effect barrier above. These instructions were not executed against the live board. Deployment and live opt-in require separate authorization.
