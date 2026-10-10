# Runtime held-intent reconciliation — current verification status

## Scope

Candidate: uncommitted worktree on `fix/runtime-dispatch-transport`, based on `bd27b9a6d88498ac694013402cd7ef7040a63b29`. No installation, deployment, live board/profile/policy mutation, gateway restart, commit, or push was performed.

## Current behavior

`POST /runtime-escalation/reconcile-held` is task-scoped and read-only with respect to native state. Under the runtime operation lock it accepts only a legacy held intent with the original immutable binding, enabled distinct configured profiles, matching lease/workspace/checkpoint, exclusive workspace ownership, and a complete native `task`/`runs`/`events` snapshot whose exact final run and event are the persisted failed run/failure event. It persists the detached snapshot and SHA-256 digest as operator no-effect evidence; it never invents a transport receipt, replaces/refunds an attempt, or sends a native effect.

A lost POST response is idempotent: later retries return the existing durable proof without a new snapshot, including after the intent progresses or returns to held. The next real tick pins a shared non-reentrant runtime-operation lock across native unblock/readback/reassignment/routing. The supported runtime-policy writer uses that same lock and fails busy rather than deadlocking if called from an effect seam. The tick rereads current authority immediately before the only unblock send, immediately before reassignment, and again before route publication. If the proof, route, ownership, binding, checkpoint, profile, lease, or pause state changed, it holds without sending the next effect. Transport/readback/reassignment failures remain held and never loop a native effect.

The joined regression models the prior-handler `Unknown tool: kanban_unblock` response with no native effect and no persisted receipt, then uses explicit recovery, a real dispatch tick, strong-worker admission, handoff, and post-review. It also retains no-refund and downstream-gate assertions.

## Historical claims versus current acceptance

Earlier version of this document reported parent-owned native proofs, a built wheel, independent review GO decisions, and a fully passing suite for another candidate/worktree. Those are historical claims only; they do **not** prove this uncommitted candidate and must not be treated as current acceptance.

Current pending acceptance requires the exact full-suite command below, review of the resulting dirty diff, and a parent-owned native proof in an environment that can initialize an isolated native board. Child contexts preserve `HERMES_DELEGATED_CHILD_CONTEXT` / `HERMES_SUPERVISED_CHILD`; they never clear the production guard. When the fresh isolated process cannot initialize the parent-owned native board, the transport regression is skipped honestly and native transport acceptance remains pending.

## Verification command

```sh
uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx --with psutil --with requests python -c "import sys;sys.path.insert(0,'/home/ocadmin/.hermes/hermes-agent');import pytest;raise SystemExit(pytest.main(['tests','-q','-o','addopts=']))"
```

## Recorded RED → GREEN evidence

- **RED:** `test_runtime_policy_writer_is_busy_during_native_unblock_authority` failed because the supported runtime-policy writer could mutate during the native effect seam; `test_reassign_final_barrier_holds_when_lease_changes_after_unblock_readback` failed because no final reassignment barrier ran (**2 failed**).
- **GREEN focused:** `tests/test_runtime_safety.py tests/test_runtime_native.py tests/test_runtime_transport.py` → **34 passed, 16 skipped**. The joined native nodes are skipped under the delegated-child native guard.
- **GREEN regression:** `test_runtime_reconciliation_recovers_lost_unblock_response_from_exact_ready_readback` plus the two new race tests → **3 passed**.
- **GREEN full:** the exact command above → **132 passed, 40 skipped**.
- **Transport fence:** `tests/test_runtime_transport.py -rs` → **1 skipped**: fresh isolated process cannot initialize the parent-owned native board; native transport proof remains pending.

## Parent verification and independent acceptance

The parent ran the exact command above against the corrected candidate without clearing child guards: **172 passed in 32.41s, zero failures and zero skips**. This includes the joined legacy held-intent recovery and fresh-process native transport regressions. `git diff --check` passed.

Independent fresh cumulative review from `3ce544b34bbd0afa774a96e96a21023706fd4582` through this corrected worktree returned **GO**, with no acceptance blockers (`deleg_9fd4d9c8`). The reviewer independently ran **132 passed, 40 skipped** in its guarded child context. Prior NO-GO findings concerning policy-writer serialization and stale reassignment authority are resolved by the shared operation lock and final reassignment/publication barriers.

The pending-acceptance language above records the earlier child handoff, not the final parent outcome. Code/native-fixture acceptance is complete. Live installation, deployment and RM03 recovery remain separate parent-owned operations; none were performed for this candidate.
