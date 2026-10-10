# Native Kanban review gate

A small Hermes plugin for native Kanban review gating. Native Kanban owns tasks, dependencies, claims, runs, review transitions, holds, and completion. Git owns source history. The plugin owns profile defaults, immutable explicit-task bindings, two model tools, and a dashboard page. There is no coordinator, background scheduler, or second task-state ledger.

## Board-wide completion gating

The dashboard discovers native boards and can publish an immutable board policy containing its activation ID, complete per-board native run-ID watermark, and pinned implementation/reviewer profiles. Publishing policy changes no native card. Its read-only policy view separates eligible no-run cards from an exact eligible active first post-watermark run awaiting its first gate, actionable operator-attention cards, done/archive history, and legacy explicit bindings. A no-run card is not yet bound, and blocked cards are never labelled ready.

For a previously unbound task, the first gated lifecycle call binds it only when native evidence shows exactly one current running implementation run, owned by the pinned implementation profile, in an absolute task-scoped `worktree:` workspace, and its native run ID is strictly greater than the activation watermark. The watermark is read in a complete read-only native snapshot immediately before policy publication: runs committed in that snapshot are pre-activation, while later committed runs qualify, including same-second runs. The binding persists native run and policy provenance before the review handoff. Direct completion/review/change lifecycle tools bind an eligible first run then refuse the direct call, so workers must use the gate tools. Pre-activation, ambiguous, prior-run, review, profile/workspace-mismatched, and malformed-policy cases fail closed with operator attention.

Policy publication and first-run admission share the plugin state lock. That serializes policy reads/publication within the plugin's supported scope; it does not claim to veto native spawning already admitted by the dispatcher.


## Requirements and installation

Use Linux, Python 3.11+, Git, and Hermes with `PluginContext.dispatch_tool`, `pre_tool_call`, native review/changes transitions, and dashboard extensions. The host supplies Hermes, FastAPI, and Pydantic. The installer needs pip; building needs the Python `build` package.

```sh
python -m build --outdir dist
python scripts/install_artifact.py dist/local_first_review-1.0.0-py3-none-any.whl --home /absolute/hermes-home
HERMES_HOME=/absolute/hermes-home hermes plugins doctor /absolute/hermes-home/plugins/local-first-review --ci
HERMES_HOME=/absolute/hermes-home hermes plugins enable local-first-review
```

The build creates a source distribution and a wheel from it. The installer materializes runtime modules, plugin metadata, and dashboard assets. It refuses an existing destination and does not enable anything. A pip entry point alone does not install dashboard assets. For source development, an intact checkout can instead occupy the same plugin-directory location.

**Install and enable the same reviewed artifact in the dashboard profile and both worker profiles.** Hermes directory-plugin discovery is profile-local. A plugin enabled only in the dashboard cannot guard a worker in another profile. Verify registration in both workers and ensure their enabled toolsets expose `local_first_review` and native worker tools. Profile selection validates existence, not installation or provider readiness. Installation/enabling is an explicit deployment action; configuration save never performs it.

Production activation is separate from isolated verification. No migration of historical cards or old coordinator state is provided.

## Configure and retain legacy bindings

Open **Review Gate** in Hermes. Choose existing profiles and save; the same profile may be selected for multiple roles. Defaults apply only to legacy explicit bindings. Existing bindings retain their profiles and workspace. Profile model, provider, credential, and auxiliary settings are never rewritten.

The **Board-wide review gating** selector publishes policy for its selected board and shows its read-only classification. Only a card with the pinned assignee, a task-scoped `worktree:` workspace, exactly one current running implementation run, and a run ID after the watermark is shown as awaiting its first gate. Running/review/prior-run, unassigned, wrong-assignee, workspace-mismatched, and blocked cards carry an actionable attention reason and are never adopted. Legacy explicit bindings remain in managed-task telemetry rather than being counted again in board-policy telemetry.

## Create isolated managed tasks

For new board-policy-managed work, create the task with a repository-root `worktree:` workspace. Native Kanban materializes a linked Git worktree at `<repo>/.worktrees/<task-id>` and persists that resolved path before spawning the worker. The first gated model-tool call binds the exact post-activation native run to that task-specific path; no pre-run enrollment or manual workspace binding is needed.

```sh
hermes kanban --board default create 'Implement the feature' \
  --assignee impl --workspace worktree:/absolute/project \
  --body 'Acceptance criteria and required checks. Finish through finish_implementation; reviewers use submit_review.' --json
```

Replace `impl`, the repository, and the body with the intended profile and acceptance criteria. Verify in `hermes kanban show TASK_ID` that `workspace_kind` is `worktree` and, after native dispatch, `workspace_path` resolves under `.worktrees/TASK_ID`. The dashboard's explicit **Enroll task** action is reserved for already-materialized linked worktrees; it refuses repository roots and shared `dir:` paths. Do not use it as a substitute for native worktree resolution. Existing bindings retain their original profiles and paths; there is no automatic migration.

Configuration is stored in `local-first-review.json` in the shared native Kanban home (`HERMES_KANBAN_HOME` for dispatcher workers). It contains defaults and bindings, not phases or verdicts.

## Worker and reviewer flow

1. The implementation worker implements the card, runs required checks, and commits its candidate. The Git worktree must be clean, including ordinary untracked files. Ignored check logs may be handed off as artifacts.
2. It calls `finish_implementation(summary, artifacts)` once. Describe changes, executed checks/results, and limitations. Optional artifacts must be existing workspace files: at most 20, each at most 16 MiB. The plugin hashes them and binds the Git revision/native implementation run in native metadata.
3. The plugin invokes native `kanban_request_review` through `PluginContext.dispatch_tool`, verifies the handoff, and tells the worker to stop. Implementation never briefly becomes done.
4. The configured reviewer claims native review. Native task history, available through `kanban_show`, carries the implementation summary, artifacts, and candidate metadata. Independently inspect source and check evidence; a summary is not proof that checks passed.
5. Call `submit_review(verdict, rationale)` with `approved` or `changes_requested` and substantive findings. Code owns task/run identity, routing, and native transition arguments.
6. Changes return ownership through native `kanban_request_changes`. Approval of the unchanged candidate/artifacts permits native completion and ordinary dependency promotion.

By default, two changes-requested cycles are allowed and a third negative verdict creates a `needs_input` operator hold. Native repeated-hold policy may route it to `triage` instead of `blocked`. Unblocking does not reset native correction history.

## Optional bounded review-correction escalation

Review-correction escalation is **off by default** and is separate from failed-run recovery. Per board, the dashboard can persist a normal substantive-correction limit (1–2), one to three escalation attempts, an implementation-capable escalation profile, and a post-escalation reviewer. Profile selection is unrestricted: the same Hermes profile may be selected for multiple roles. Saving or enabling this policy changes no native task: it applies only when a **future** reviewer exhausts the configured normal correction budget. Existing held cards are never swept, unblocked, or rerouted; recover one only through deliberate native/operator action.

At exhaustion, the current reviewer’s exact run, candidate, immutable original binding, configured routing, correction count, and attempt number are written to the plugin ledger before any native effect. The plugin then uses native `kanban_request_changes`, reads it back, uses Hermes' supported `reassign_task` operation to route the same ready card to the escalation implementation profile, reads that back, and only then publishes separate effective routing. If the exact target claims the card between reassignment and readback, its task/run/profile/phase, workspace, candidate handoff, immutable binding, and preceding review transition are reconciled as the same route; old, wrong-profile, or unproven claims remain fenced. Original binding/provenance is never rewritten. A lost response is reconciled from exact native events/readback without a blind resend; a mismatch or missing profile fails closed for operator inspection.

Disabling escalation pauses a **pending** intent before watchdog reassignment, routing publication, or worker admission. The consumed attempt and durable intent remain visible as held and are not refunded or discarded; re-enabling can reconcile that exact intent. It does not revoke an already published/active native route.

The escalation implementation worker produces a new candidate through the normal handoff. The configured reviewer submits a fresh verdict through a separate native worker session; profile identity does not restrict role selection. A negative post-escalation review consumes the bounded attempt. Once attempts are exhausted, or routing/verification is uncertain, the card is held for an operator—there is no fallback to ordinary correction cycles and no escalation chain. This policy does not react to crashes, provider failures, or runtime watchdog budgets.

Duplicate finishing after handoff is refused. A lost transition response triggers native task/run/event readback without resending. An unproven outcome returns a visible error; inspect native history before retrying. Missing profiles, changed workspace/candidate, or changed artifacts prevent approval.

## Enforcement boundary

The pre-tool hook refuses managed direct `kanban_complete`, `kanban_request_review`, and `kanban_request_changes` before their handlers. Unmanaged cards retain native behavior. Normal worker exit without a valid transition is a native protocol violation/retry/hold, not completion.

## Optional runtime-exhaustion escalation

Runtime-exhaustion escalation is a second, **separate opt-in**. It never treats a crash, spawn failure, ordinary timeout, reviewer verdict, or manual hold as a trigger. It is eligible only after the existing implementation phase has already consumed its configured failed-run recovery budget and the next exact native failure is a started-worker, iteration-budget `gave_up` record with the same RM02 evidence required by recovery.

For that one task and failed run, the plugin persists the immutable original binding, workspace checkpoint, exhausted phase identity, and target implementation/reviewer route before any native effect. The next dispatch tick may unblock the **same card** and use native reassignment to route its existing task-scoped worktree to the configured implementation profile; it reads both transitions back before publishing effective routing. The implementation worker receives the normal fresh handoff path and the configured distinct reviewer remains required for approval. A pending/lost response is reconciled from exact native readback; a conflicting claim, changed workspace/checkpoint, missing profile, pause, or ambiguous state remains held without a duplicate unblock/reassign.

This policy has no historical sweep. Enabling it does not adopt old blocked cards. Explicit adoption remains limited to the one RM02-shaped, post-watermark unbound failure described below. Runtime escalation consumes no recovery grants and never refunds the two already-spent phase recoveries.

## Optional autonomous failed-run recovery

Recovery is **off by default** and has explicit dashboard enable/pause controls per board. When enabled, the installed plugin's real `on_kanban_dispatch_tick` hook may adopt only one unbound RM02-class card: exactly one post-watermark implementation run, terminal `gave_up`, a same-run native `spawned` record, `trigger_outcome: timed_out`, `effective_limit: 1`, matching positive `budget_used`/`budget_max`, and the native `Iteration budget exhausted (...)` error classification. This is intentionally the started-worker 180-iteration exhaustion class evidenced by RM02 run 2247—not `spawn_failed`, ordinary wall-clock timeout, a manual hold, or another historical failure. It also requires the exact implementation `claimed` source phase. It refuses every other historical card. Before supported native unblock it atomically persists the immutable binding, bounded Git checkpoint, identity `(board, task, failed-run, phase)`, one intent, a durable phase budget, and a shared-workspace lease. A review failure resumes review and retains the review handoff.

A successful native `kanban_unblock` resets Hermes' native failure counter, so it is never the watchdog budget. The board policy's **Maximum failed-run recoveries per phase** defaults to **1** and accepts only whole numbers from **1 through 5**; use **Pause failed-run recovery**, not zero, to stop automation. The limit is separate for implementation and reviewer phases, and each phase's count is shared by all failed runs of that task. Every distinct failed-run identity consumes one durable grant before unblock/claim; pause/re-enable, terminalization, and restart never refund it. Lowering a bound holds future grants without revoking an already-authorized replacement; raising it permits only a later eligible failed run. It reads native state before a resend and only reconciles a lost response to the exact persisted intent. Leases release only after a terminal native observation.

The first **normal model tool** in the exact next native claim rechecks board/profile/workspace/run identity, expected claimed source phase, and checkpoint, then pins an admission receipt. Until then every normal model tool—and a recovery-state read error—is fail-closed. After receipt it deliberately does not compare the old checkpoint again: admitted workers are expected to edit. This is an admission boundary, not a pre-process sandbox.

Use the dashboard recovery controls to configure the per-phase limit and enable or pause recovery after reviewing its preview. Restart the normal gateway dispatcher after installation/plugin changes. If `kanban_unblock` is unavailable to the hosting plugin context, recovery remains fail-closed with its durable intent; use native Kanban inspection rather than manually editing plugin state.

This is a model-tool policy gate, **not a shell/database/operator sandbox**. Native CLI/dashboard completion, privileged dispatch from another plugin, a hostile shell-capable worker, or a worker without this enabled plugin can bypass it. Supported managed workers must load the plugin, preserve dispatcher context, use its tools, and stop edits after handoff. Do not run other source writers against the candidate during review. Git-ignored files not named as artifacts are outside candidate evidence; commit source/check definitions and explicitly name relevant generated evidence. No core patch or shell-text filtering is used.

## Dashboard and troubleshooting

The page offers selectors, explicit enrollment for an already-materialized worktree, and read-only native snapshots. It distinguishes lane, inferred phase, native run lifecycle, and process liveness. Process liveness is **unknown, not probed**. Refresh runs every 15 seconds; stale/unavailable warnings mean retained values are not current activity or success.

Counts describe scoped native states, not throughput or OS process counts: implementation claims, queued/claimed reviews, work awaiting correction, verified approved/done cards, and failures/holds. Unknown counts are not zero. Cards show board/task identifiers, bound profiles, summaries, findings, observation time, and next steps. Reads never dispatch or transition work.

- **Missing profile:** restore the bound profile; changing defaults does not reroute existing work. Verify plugin registration/provider readiness before release.
- **Enrollment refused:** new board-policy work should use `--workspace worktree:<absolute-repo-root>` and native dispatch; first-run binding is automatic before model tools. Explicit enrollment rejects tasks without a concrete linked worktree, no prior native runs, and an operator-parked `needs_input` state. Executed work is not normally adopted; the sole exception is the narrowly evidenced RM02 started-worker iteration-exhaustion class above. A `needs_input` hold is always an operator decision and is never auto-retried.
- **Stale candidate/evidence:** inspect changed files and native history; do not approve a candidate different from the handoff.
- **Worker failure/ordinary exit:** inspect `hermes kanban --board BOARD show TASK_ID`, `hermes kanban --board BOARD runs TASK_ID`, and `hermes kanban --board BOARD log TASK_ID`. Recover through native operations without forcing completion.
- **Review limit/escalation:** inspect the latest findings and persisted routing. With escalation disabled or its attempt budget exhausted, no further automatic correction is granted; resolve the hold deliberately through native Kanban.
- **Telemetry unavailable:** check board/task identity, profiles, configuration, and filesystem access. A retained snapshot is not current activity.

## Development

```sh
python -m pytest tests -q -o 'addopts='
hermes plugins doctor . --ci
node --check dashboard/dist/index.js
```

Tests need Hermes importability plus pytest/FastAPI/httpx. Native fixtures use disposable Git workspaces/boards without a provider. Run them from a non-delegated parent: delegated contexts intentionally skip native mutation fixtures, preserving Hermes' guard. Report skips separately.

For standalone probes, pin **HOME, HERMES_HOME, and HERMES_KANBAN_HOME** outside production and disable gateway dispatch. Temporary HERMES_HOME beneath the real home alone does not isolate profile discovery.

Controlled native tests, installed-artifact checks, actual dashboard/browser proof, independent exact-source review, and provider-backed worker rehearsal are separate verification levels. Keep raw logs and acceptance evidence outside the product tree. Controlled callback dispatch is not model-generated review or real-worker rehearsal. The escalation fixtures are synthetic disposable-native evidence only; they do not activate the policy on any production board.
