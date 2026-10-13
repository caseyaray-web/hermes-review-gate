# Autonomy acceptance evidence matrix

## Scope and result status

This is a source-and-documentation audit only. It records what the current tests *intend to exercise*; it does not execute them, alter production state, import the plugin into a live profile, contact a provider, or change a board/worktree.

- **Baseline (reported, not rerun here):** 218 passed in Docker using copied Hermes `46904a3b`.
- **Current candidate (reported, not credited as passing):** 217 passed with four stable-hold failures under repair by another agent. The worktree is concurrently dirty (`local_first_review/native.py`, `plugin.py`, `runtime_escalation.py`, `tests/test_review_gate.py`, `tests/test_runtime_native.py`, `tests/test_runtime_safety.py`, plus untracked `tests/test_stable_holds.py` and this document). These references are not pristine-candidate acceptance evidence.
- **No provider-backed autonomy is accepted.** The fixtures use literal model-tool arguments and synthetic worker PIDs/processes; they do not obtain a model response or make a provider request.

## What “native” means here

The `board` fixture in `tests/test_review_gate.py:15-111` creates an isolated native Hermes Kanban SQLite board under pytest `tmp_path`, dispatches through `kbd.dispatch_once()`, and reads native task/run/event state. It writes profiles whose default model is `unused-controlled-fixture` (`:29-45`) and invokes tools literally through `model_tools.handle_function_call()` (`:87-89`). Thus, a joined test is controlled native transition coverage, not an actual agent/provider rehearsal.

Worktree isolation is **cross-cutting fixture/infrastructure coverage**, not Scenario 1: `tests/test_review_gate.py::test_two_tasks_from_same_repo_get_distinct_native_worktrees` (`:114-136`) proves two native tasks receive distinct linked worktrees, branches, and file isolation.

## Exact eight-scenario matrix

| # | Required user sequence | Correct current reference(s) | What the reference establishes | Boundary / remaining gap |
|---|---|---|---|---|
| 1 | **Implementation succeeds; reviewer approves completion.** | Closest joined lifecycle reference: `tests/test_review_gate.py::test_registered_dependency_correction_and_approval` (`:139-178`), especially `finish_implementation` at `:146`, reviewer approval at `:174-177`, and `done` at `:176`. | A registered implementation handoff and independent-review approval complete the native card. | **Not an isolated no-rejection happy-path test:** this test includes an intervening rejection/correction. It is controlled native/tool invocation only; no model/provider worker. |
| 2 | **Reviewer rejects; implementation corrects; a fresh reviewer approval completes.** | `tests/test_review_gate.py::test_registered_dependency_correction_and_approval` (`:159-178`); also `::test_native_board_policy_watermark_binds_never_run_and_future_cards` (`:210-226`). | The reviewer requests changes, the implementation commits a correction and hands off again, then a fresh approval makes the task `done`. The second reference additionally exposes the dependent’s waiting state before that approval. | Controlled lifecycle proof only; model behavior and real review quality are not exercised. |
| 3 | **Iteration is exhausted; automatic local continuation succeeds.** | `tests/test_review_gate.py::test_native_rm02_started_iteration_exhaustion_is_adopted_and_admitted` (`:649-738`). | Starts a disposable process, records the native `gave_up`/iteration-budget evidence, uses the normal recovery tick and same task/worktree, commits the preserved partial work, hands off, and receives normal review approval. It asserts the task-local `max_retries=1` contract and one charged implementation recovery budget. | The iteration exhaustion is injected through `_record_task_failure`; it is not a provider model consuming an iteration budget. |
| 4 | **Repeated exhaustion; automatic paid escalation; independent approval.** | `tests/test_review_gate.py::test_joined_runtime_exhaustion_escalates_same_card_after_two_recoveries` (`:958-1030`). | After two normal local recovery grants, the third exact exhaustion automatically routes the same card/worktree to `strong` (the Terra/paid-route fixture role), preserves immutable binding and recovery budget, then routes review to `post-review`, which approves. | “Paid” is represented by profile/route names only. No provider request, billing event, or actual paid-model worker is proved. **Explicit operator retry is not evidence for this automatic sequence.** |
| 5 | **Paid escalation fails review; stable hold.** | `tests/test_runtime_native.py::test_joined_negative_post_escalation_review_holds_without_another_coder` (`:463-480`). | The runtime-escalated `strong` implementation hands off to `post-review`; a negative review leaves the same task blocked with runtime intent `held`, does not dispatch another coder, and leaves the dependent `todo`. `::test_joined_failed_terra_attempt_stays_held_without_new_budget` (`:482-493`) is the failed-escalated-implementation companion. | Controlled native fixture only. **Operator retry is a separate, explicit manual continuation path and must not substitute for this automatic stable-hold result.** |
| 6 | **Gateway/dispatcher restart; isolated recovery; no duplicate effects or budget spend.** | Partial persistence reference: `tests/test_review_gate.py::test_joined_configured_recovery_limit_allows_two_failed_run_identities_then_holds_after_restart` (`:741-805`). It reloads durable state with `state.load_state()` and calls watchdog ticks after the simulated boundary. | Durable recovery-budget accounting is checked across fresh state reads: two grants are retained and the third failure is held. | **Missing actual process restart proof.** This does not stop/start a gateway or dispatcher process, nor prove isolated restart recovery/no duplicate native effect across that process boundary. It is a same-process state reload/tick simulation, not a gateway/dispatcher restart. Do not call it module reload or process restart. |
| 7 | **Lost native unblock; exact reconciliation or hold.** | Joined lost-effect route: `tests/test_runtime_native.py::test_joined_lost_response_restart_never_repeats_native_effect` (`:192-215`), which injects loss after unblock or reassignment and asserts exactly one unblock/assignment route. Exact public held reconciliation: `::test_joined_held_unknown_transport_reconciliation_resumes_same_attempt` (`:126-189`), including one native unblock/assignment and idempotent replay. Fail-closed ambiguous-readback supplement: `tests/test_runtime_safety.py::test_operator_retry_lost_response_with_ambiguous_readback_holds_without_resend` (synthetic unit test). | Joined fixtures exercise native board readback and prevent a second effect for their modeled losses; the public held-reconciliation flow reads native evidence and retains the same attempt. The safety unit test covers the hold-without-resend branch only with mocked snapshot/dispatch. | Despite its name, `test_joined_lost_response_restart_never_repeats_native_effect` does **not** launch a fresh process; it calls `state.load_state()` and another tick in-process. It is neither gateway/dispatcher restart proof nor provider transport proof. |
| 8 | **Dependent remains blocked until approved completion.** | `tests/test_review_gate.py::test_registered_dependency_correction_and_approval` (`:141-178`): `b.gated()` asserts the child is `todo` through implementation/review/correction; only after approval does `b.dispatch().id == b.child` (`:178`). Independent corroboration: `::test_native_board_policy_watermark_binds_never_run_and_future_cards` (`:210-226`). | The native dependent cannot dispatch before the predecessor’s approved completion and becomes dispatchable only afterward. | Controlled native dependency proof only; no provider worker or live production board. |

## Classification and restart terminology

- **Joined native lifecycle evidence:** the references above use the disposable SQLite board and native dispatcher/tool paths, except where explicitly marked as a synthetic supplement.
- **Synthetic-only safety supplements:** `tests/test_runtime_safety.py` monkeypatches native snapshots, dispatch, and/or reassignment. These are useful fail-closed tests but cannot independently prove a native SQLite effect.
- **Fresh interpreter/module-load evidence is narrower than a service restart:** `tests/test_review_gate.py::test_ambiguous_handoff_native_readback_and_restart` (`:495-520`) launches a fresh Python interpreter to load the plugin and reconcile one already-committed review handoff. `tests/test_runtime_transport.py::test_fresh_cli_context_registers_native_unblock_after_registry_cached` (`:12-93`) launches a fresh isolated CLI process for tool-registration/transport coverage. Neither is a stop/start of the production gateway/dispatcher, and neither completes Scenario 6.
- **Operator retry is supplemental:** `tests/test_runtime_native.py::test_joined_operator_retry_claims_exact_latest_window_after_historical_failures` (`:234-287`) verifies a public, explicitly authorized manual retry and exact claim admission. It does not prove automatic paid escalation (Scenario 4) or the automatic post-escalation stable hold (Scenario 5).

## True missing acceptance evidence

1. An isolated joined Scenario-1 happy-path test: implementation handoff followed directly by independent approval and completion, without the Scenario-2 rejection/correction path.
2. A real provider-backed implementation worker that receives a response, uses normal tools, commits a candidate, invokes `finish_implementation`, and is independently reviewed.
3. A provider-backed iteration-budget exhaustion and automatic local-continuation run.
4. A provider-backed paid-route escalation/review run, including the claimed provider/billing boundary if “paid” is an acceptance requirement.
5. A real gateway/dispatcher stop-start recovery experiment in isolated homes that proves no duplicate effect and no extra budget spend; durable in-process state reload is insufficient.
6. A Docker-isolated copied-Hermes rerun of the current exact candidate, with native-fixture skip reasons recorded, after the stable-hold repair. The reported baseline does not validate this dirty candidate.

## Provider-rehearsal feasibility: read-only metadata inspection

No credentials were read or emitted, no model endpoint was contacted, and no profile was changed. Read-only configuration metadata previously recorded the following role selections:

| Documented role | Profile metadata observed | Rehearsal conclusion |
|---|---|---|
| Local implementation | `worker-code-local`: `custom:lm-studio`, model `qwen3.8-27b@iq3_s` | Metadata supports a possible local-provider rehearsal, but not reachability, credentials, tool-call compatibility, capacity, or plugin enablement. |
| Ordinary review | `worker-architect-sol`: `openai-codex`, model `gpt-6.1-sol` | Provider selection metadata only; no request, quota, credential, or installed-plugin proof. |
| Runtime-escalated implementation | `worker-code-terra`: `openai-codex`, model `gpt-6.1-sol` | Same limitation. |
| Post-escalation review | `worker-review-terra`: `openai-codex`, model `gpt-6.1-sol` | Same limitation. |

`README.md` separates controlled callback/native tests from provider-backed rehearsal and warns that profile names do not establish provider readiness. Provider-backed autonomy remains an outstanding acceptance gate.

## Mandatory isolation for future execution

Run future evidence in a Docker/containerized copy of Hermes pinned to `46904a3b`, with a copied candidate tree and all of `HOME`, `HERMES_HOME`, and `HERMES_KANBAN_HOME` inside container-owned temporary paths. Disable gateway dispatch for fixture-only runs. Do not mount or reference production boards, profile homes, worktrees, or plugin directories. Preserve the delegated-child native-mutation guard; record a skip as missing parent-context proof rather than relaxing the guard.

## Acceptance conclusion

The tests provide substantial **controlled** native SQLite coverage for correction/review gating, local recovery, automatic runtime escalation, durable state, and lost-effect fencing. They do not demonstrate actual-model autonomy, paid-provider execution, or a real gateway/dispatcher restart. The baseline is historical only, and the dirty current candidate with reported stable-hold failures is not accepted.
