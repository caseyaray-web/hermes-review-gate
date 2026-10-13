# v11 autonomy stabilization verification

## Candidate and scope

Base: `1efb72ffd8c21379922fc24459f2f6b25c0c831d`, branch `fix/v11-autonomy-stabilization`.
Development only. No deployment, production configuration changes, service actions, ticket transitions, ledger migration or budget changes were performed in this stabilization assignment.

## Implemented

- A: Guard-delivered continuation envelope distinguishes verified current run/profile/workspace/route from sealed original contract, findings, checkpoint and explicitly historical retry evidence. One-use delivery and admission checks remain.
- B: At native needs-input recurrence boundary, use supported task-scoped scheduling rather than promoting an exhausted operator hold to triage. No global decomposition setting change; dependencies remain native.
- C: Record lost operator-retry unblock response, perform exact native readback, reconcile a unique authorized effect without resend, hold ambiguity.
- Durable state schema remains v11; state.py is unchanged from this branch base.

## Executed evidence

Parent executed full baseline suite: 218 passed.
Parent executed complete candidate suite after all test additions: **224 passed, zero skips**, one Starlette TestClient deprecation warning, 33.75 seconds.
Full output: `/home/ocadmin/.hermes/cache/scratch/review-gate-autonomy-tests-4.log`.
Independent A/B/C code/requirements review: GO, no blockers. Review was read-only and relied on parent test execution. Dispatcher restart test was subsequently verified by the parent full suite.

Command:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /scratch:rw,uid=1000,gid=1000,mode=700 \
  -v /home/ocadmin/Documents/hermes-review-gate-autonomy:/candidate:ro \
  review-gate-autonomy-test python -B -m pytest -p no:cacheprovider -q -rs --tb=short
```

Image includes archived Hermes core `46904a3b467f62616f5b3ee247adce30b1b277a0`. No production homes, boards, credentials, socket or service bus mounted. Runtime test networking disabled. Only candidate checkout mounted read-only; all fixture state under container scratch. Image build requires package-download networking.

New joined native tests cover continuation guard packet delivery, operator-retry lost response with immediate fresh claim, stable repeated holds, and actual dispatcher process restart. The restart regression runs two fresh interpreters with native dispatch and registered recovery hooks, verifying one unblock, one consumed budget and no dependent release. It disables replacement spawning; it is not a full messaging-gateway or real-model crash rehearsal.

## Provider-backed limitation

No Qwen or paid model executed in this evidence run. A read-only registry request confirmed the configured Qwen model is available. The bounded actual Hermes/provider turn was blocked by an external security-approval gate for the plaintext HTTP/raw-IP endpoint; it was not dispatched. See `PROVIDER_REHEARSAL.md`. Approval of that gated request is required before claiming real-model rehearsal evidence. Existing native tests use controlled model/tool inputs and real disposable SQLite/worktree/native lifecycle operations. Provider metadata alone is not readiness or autonomy evidence. An actual-provider rehearsal requires a separately prepared network-enabled disposable environment with scoped credentials and candidate plugin installation; production profile homes must not be mounted. Provider-backed unattended completion remains unverified. Do not label this result proof of production readiness or all end-to-end acceptance criteria.

## Production invariance

Most recent read-only check: live ledger SHA-256 remains `d2cab471b71e718d80704b7fd08fefe96dd8ff320152400ffb6271647b9824c1`. Held `default:t_57851039` was not modified. This does not claim a snapshot of all independently running production activity.

`AUTONOMY_VERIFICATION.md` is the earlier source-coverage inventory; historical pending/count statements there are superseded by this execution report, not by provider-backed evidence.
