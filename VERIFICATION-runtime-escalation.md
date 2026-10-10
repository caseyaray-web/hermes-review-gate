# Runtime held-intent reconciliation — current verification status

## Scope

Candidate: isolated worktree on `feat/single-action-held-recovery`, based exactly on canonical `main` at `eee7636ea38ea6dece04a5fd2be6bffa7a7046aa`. No installation, deployment, live board/profile/policy mutation, gateway restart, push, or RM-03 worktree change was performed.

## Current behavior

`POST /runtime-escalation/resume-held` is the single operator action. It proves the legacy held intent under the existing exclusive runtime-operation lock, writes task-scoped no-effect evidence bound to the immutable binding, failed run, dirty checkpoint, complete snapshot and digest, then invokes the supported runtime reconciliation path. Reconciliation reacquires the same lock and rereads authority immediately before its native effects. A race or failed readback fails closed; a retry either returns the already-routed intent without recapturing evidence or repeating effects, or refuses a previously re-held/ambiguous intent. `POST /runtime-escalation/reconcile-held` is retained as a deprecated alias to the same one-action behavior; it no longer stages a separate operator step.

The operation preserves the original held reason/history, binding, attempt count and recovery budget. It never synthesizes the old missing transport receipt; any transport receipt after resumption is recorded only from the actual supported native operation. Successful response requires native route readback. The normal dispatcher handles the single newly authorized run; the endpoint does not spawn a worker directly. Strong-worker admission pins the exact fresh implementation run, and the existing independent review gate remains in force.

The current RM-03 incident is intentionally **not cleared**. The complete native snapshot contains a later run (2275) after the original recorded failure. Native task/run/event data records a protocol-failure and worker output stating that tools were unauthorized, but does not provide a structured authoritative per-tool/effect receipt for that run. A worker-output statement and unchanged final dirty checkpoint cannot prove that no implementation effect happened. The new action therefore refuses with the later-run proof failure and leaves native state unchanged. Do not retry or bypass this gate until the native model can supply authoritative effect evidence.

The joined regression models the old `Unknown tool: kanban_unblock` no-effect hold, then uses the public one-action POST, a real native dispatcher tick, strong-worker admission, implementation handoff, and independent post-review. It checks same binding, no attempt refund, actual effect evidence, read-free idempotent replay, and downstream availability only after approval with a separate dispatch/release.

## Acceptance and verification

The preceding parent proofs, installed artifact, and independent GO decision recorded below refer to the canonical implementation before this branch. They are historical only and do **not** review this candidate.

This candidate was exercised in the parent-owned native fixture context with child guards intact. It does not contact the live RM-03 task. Independent review is still required before any rollout.

## Verification command

```sh
uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx --with psutil --with requests python -c "import sys;sys.path.insert(0,'/home/ocadmin/.hermes/hermes-agent');import pytest;raise SystemExit(pytest.main(['tests','-q','-o','addopts=']))"
```

## Recorded RED → GREEN evidence

- **RED:** The joined regression failed at the missing `/runtime-escalation/resume-held` operation. The later-run refusal regression failed because `resume_held` did not exist. The legacy alias regression failed with HTTP 409 because it still performed proof-only reconciliation.
- **GREEN focused:** the joined native end-to-end path, later-run refusal/no-mutation test, single-action API success/refusal tests, and deprecated alias test → **5 passed**.
- **GREEN full:** exact command above → **175 passed, 0 skipped, 1 Starlette/httpx deprecation warning, 31.79s**.
- `git diff --check` passed. These tests ran with native fixture setup and without clearing child guards.
- **Live RM-03:** no request was made to resume, reconcile, dispatch, or mutate task `default/t_57851039`. Run 2275 remains later than the recorded original failure, so the available no-effect proof cannot authorize resumption; an authoritative per-tool/effect trace is still missing.

## Prior canonical implementation verification (historical only)

The following parent verification and independent review applied to canonical main before this branch. They do not establish review acceptance for this change.

The parent ran the prior exact command against the corrected canonical candidate without clearing child guards: **172 passed in 32.41s, zero failures and zero skips**. This included the earlier joined legacy held-intent recovery and fresh-process native transport regressions. `git diff --check` passed.

The prior independent cumulative review from `3ce544b34bbd0afa774a96e96a21023706fd4582` through that candidate returned **GO**, with no acceptance blockers (`deleg_9fd4d9c8`). Those findings concerned the canonical implementation’s policy-writer serialization and reassignment authority barriers.

This branch has not been independently reviewed or rolled out. No install, deployment, push, gateway restart, or live RM-03 recovery was performed.
