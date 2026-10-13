# Provider rehearsal record

## Scope

This is a bounded, synthetic continuation-packet rehearsal only. It does not create or modify a production Kanban board, ticket, profile, service, ledger, or candidate source/test file. The intended container specification was read-only candidate mount, tmpfs `/scratch`, non-root UID 1000, dropped capabilities, `no-new-privileges`, PID/memory/CPU limits, and a single configured OpenAI-compatible endpoint.

Candidate observed: `1efb72ffd8c21379922fc24459f2f6b25c0c831d`. It was already dirty before this rehearsal; no code or test files were changed by this work.

## Reachability result

A bounded unauthenticated `GET http://100.86.56.101:1234/v1/models` succeeded. The returned registry included the exact requested ID:

```json
{"id":"qwen3.8-27b@iq3_s","object":"model","owned_by":"organization_owner"}
```

The isolated image `review-gate-autonomy-test:latest` contains a copied Hermes runtime under `/core`, including `hermes_cli` and `run_agent`; its `hermes chat` command was available through `python3 /core/hermes`. The image runs as UID/GID 1000 and has Python 3.11.17. Thus an actual Hermes CLI invocation was technically feasible without reading production credential or profile homes.

## Approved retry: actual inference result

After the operator explicitly requested a retry, the normal smart-approval mechanism approved the same bounded Docker request. One actual chat-completions request executed against `http://100.86.56.101:1234/v1/chat/completions`, model `qwen3.8-27b@iq3_s`. Response ID: `chatcmpl-fpcqvzbx50vi2lh5oh4w5p`.

The visible response correctly identified run `2275`, profile `strong`, and the synthetic workspace, distinguished historical H-2274 from current admission, and began its next action with inspection of preserved uncommitted changes. The response was truncated (`finish_reason=length`) at the authorized 384-token output cap, so a complete next-action answer was not obtained. No tools executed and no paid model was called.

This is a direct real-Qwen synthetic-context comprehension probe, not a Hermes agent run, generated plugin-envelope test, implementation/review lifecycle, or unattended-completion proof. Candidate code and production paths were not mounted in this probe. Container defaults were used for user/network; it had a read-only root filesystem, dropped capabilities, no-new-privileges, resource limits, and scratch tmpfs. No claim of destination-enforced network isolation is made.

## Earlier inference attempt (superseded by approved retry)

No inference request was dispatched. The prepared one-turn Hermes invocation was blocked by the execution environment's explicit security approval gate because it contained the user-supplied raw-IP, plaintext-HTTP endpoint. The invocation remains unexecuted; no provider response, tool call, native lifecycle event, or completion is claimed.

The unexecuted synthetic prompt would have asked Qwen to choose exactly one next action from this bounded contract:

- Historical blocked run `H-2274` is context only and grants no authority.
- Sole current authority: `board=synthetic`, `task=demo-17`, `run=2275`, `profile=strong`, workspace `/synthetic/worktree/demo-17`.
- Required behavior: preserve the dirty checkpoint; inspect and continue existing changes; do not accept or clean it.

Accordingly, there is no model output to assess as correct or incorrect. This is **not** end-to-end autonomy evidence and must not be represented as native completion.

## Larger-cap probe and lifecycle continuation preflight

The operator-authorized repeat used the same synthetic prompt and configured Qwen model with `max_tokens=1024`. Actual response ID: `chatcmpl-dsz2cn73k4f6tsdftu7xme`. It finished normally (`finish_reason=stop`), using 469 output tokens including 299 reasoning tokens. The visible answer identified run 2275/profile strong, distinguished the historical block, and proposed inspecting preserved changes, correcting the bound, testing and requesting independent review. There were no tool calls. This was a direct chat-completions request, not a native worker execution.

The subsequent request to continue a native lifecycle is blocked at authorization/workspace reconciliation: `synthetic/demo-17`, run `2275`, and `/synthetic/worktree/demo-17` were literal hypothetical prompt values, not a created native ticket, claimed run, repository or preserved worktree. The actual executed probe command created none of these and supplied no tools. Its `--rm` container and scratch tmpfs were ephemeral. Inspection found no remaining rehearsal container and the documented workspace does not exist on the host. No native authorization or review receipt can be inferred from the model repeating the prompt.

Completed actual evidence: two Qwen responses (first truncated, second complete). Actual coding tool calls: none. Resulting code changes/tests: none. Independent review receipts: none. Final native task state: not applicable; no native rehearsal ticket was created. No production or candidate code changes were made during this preflight.

Required next prerequisite: initialize a fresh persistent disposable native board/task, task-scoped Git worktree with concrete buggy code and tests, and isolated worker/reviewer profiles. Obtain actual native task/run IDs through supported creation/dispatch; never force the hypothetical IDs into storage or fabricate previous failure history. The user must confirm this is a new lifecycle rehearsal, not continuation of an existing workspace. Git `user.name` and `user.email` are absent in the candidate checkout; candidate commit remains separate, while a disposable implementation commit also needs an explicitly chosen fixture author identity before managed handoff. Paid review additionally needs scoped provider access within the existing authorization, without mounting production homes.

## Earlier limitations (superseded where stated above)

- Endpoint registry reachability does not establish chat-completions/tool-call compatibility or sustained provider availability.
- No safely scoped credential was required for the registry probe, but the provider-backed Hermes turn was not permitted by the external command-approval gate.
- The default Docker bridge network was the available container network mode. The planned process would have contacted only the supplied endpoint, but bridge networking alone is not a host-enforced destination allowlist.
- No production profile, home, board, ticket, service, or ledger path was mounted or used.

## Persistent native lifecycle rehearsal — 2026-10-12

A new disposable-only root was created at:

`/home/ocadmin/.hermes/cache/scratch/new-native-provider-rehearsal/run-20261012T183249Z-Nbg0oi`

The candidate was read-only at `/candidate`; an exact writable staging copy was used solely to build `local_first_review-1.0.0-py3-none-any.whl` (wheel SHA-256 `27b69841fbe9695b47f3ed02266f750de6ce856a21c96d75d0db328e49887a78`). Candidate `plugin.yaml` before/after staging SHA-256 was `025bafac3dc72598dadca74b7219be16d5ecf08ecbd34f422e727bee57b548af`. No candidate source/test file was edited; this record is the sole candidate edit.

Each container used `--read-only`, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit 256`, `--memory 2g`, `--cpus 2`, tmpfs `/tmp` and `/run`, and only the disposable root writable. It ran UID/GID 1000; `/home/ocadmin/.hermes` and `/var/run/docker.sock` were absent. The image exposes the Hermes source CLI as `python3 /core/hermes_cli/main.py` rather than a `hermes` executable. That native CLI, Git, Python, FastAPI, Pydantic, and pytest were verified. `Plugin Doctor --ci` passed after documented artifact installation (2 tools, 3 hooks), and the plugin was enabled in disposable default, `rehearsal-coder`, and `rehearsal-reviewer` homes. The image warned that optional ffmpeg/node/npm/ripgrep were out of sync; this did not fail Plugin Doctor.

A disposable Git fixture was initialized with identity exactly `Hermes Rehearsal Fixture <fixture@hermes.invalid>`, initial commit `8d82895a75462571836409a29ed5ddb7f84bc467`, intentional `add()` subtraction defect, and three immutable pytest acceptance tests. Baseline isolated pytest result: `3 failed` (the expected defect). No implementation fix was made.

Supported native operations created board `rehearsal-lifecycle`, task `t_0bf62bfd`, and native run `1`. The ticket has `workspace_kind=worktree`, `workspace_path=/fixture/.worktrees/t_0bf62bfd`, assignee `rehearsal-coder`, exact requested model `qwen3.8-27b@iq3_s`, provider override `custom:qwen`, and `max_retries=1`. Actual `kanban dispatch --dry-run --json` selected it; actual `kanban claim t_0bf62bfd` materialized and claimed the task-scoped linked worktree. A separate fresh container then ran actual `kanban dispatch --json` and native show/runs readback: it observed the same persisted running task/run and spawned no duplicate worker. This is a safe process-boundary persistence proof, not a gateway service restart proof.

The first Hermes Qwen worker launch was attempted with task/board environment and terminal/file/kanban tools but was refused before inference: the discovered installed schema requires `custom_providers` to be a YAML list, whereas the initial CLI write had made it a mapping. Exact refusal: `Unknown provider 'custom:qwen'` and `custom_providers is a dict — it must be a YAML list`. No model request or tool call occurred; the fixture remains defective and uncommitted.

A supported CLI correction to list-schema provider configuration plus a real tool-capable Qwen sentinel was submitted through the normal approval gate. The gate returned `pending_approval` for the raw-IP plaintext endpoint request. It was not retried or bypassed. Consequently no Qwen coding/testing run, native review dispatch, reviewer claim, paid independent review, review receipt, or completion has occurred. The persistent task is deliberately left `running` with run `1`; do not treat this as lifecycle completion.

## Reconciliation after pending sentinel — 2026-10-12T18:52Z

Readback found no evidence that the pending tool-capable Qwen sentinel ever executed: `logs/qwen-coder.txt` contains only the earlier schema refusal, the disposable coder/reviewer `agent.log` files are empty, no rehearsal worker/container process exists, and native event history still contains only `created` and `claimed`. Endpoint inventory was reachable and included `qwen3.8-27b@iq3_s`; this is non-inference reachability only.

The disposable `rehearsal-coder` and `rehearsal-reviewer` profiles were corrected through supported `hermes -p <profile> config set --force custom_providers ...` only. CLI readback now reports a list containing `{name: qwen, base_url: http://100.86.56.101:1234/v1, api_mode: chat_completions}` and retains `model.provider=custom:qwen`, `model.default=qwen3.8-27b@iq3_s`. `hermes doctor` still reports that this is a legacy list entry and recommends the newer `providers:` store; no cross-home or production configuration was touched.

A fresh isolated native readback used UID/GID 1000, read-only container root, `--cap-drop ALL`, `no-new-privileges`, candidate mounted read-only, root mounted only at `/rehearsal`, and `HERMES_HOME=/rehearsal/home`, `HERMES_KANBAN_HOME=/rehearsal/home/kanban`. It observed the same task `t_0bf62bfd`, run `1`, `status=running`, `worker_pid=null`, `ended_at=null`, no summary/error/metadata, and no `spawned` or `worker_registered` receipt. The original claim expires at `2026-10-12T18:54:46Z`; the reconciliation observation was at `2026-10-12T18:52:18Z`. The only supported operator recovery surface is `kanban reclaim`, which releases the active claim and terminates an owned process if present; normal launch is `kanban dispatch` after a ready task.

**Concrete stop blocker:** the review-gate plugin can bind only one exact active first native implementation run. Its persisted-contract check rejects `len(runs) != 1` and an ended/non-running first run. Reclaiming this no-PID run would create prior/reclaimed run history and require a later run, making first-run adoption ineligible. The worker-side `adopt_worker_pid` API is not an operator recovery API and requires an already spawned worker. Therefore no reclaim, redispatch, manual PID adoption, replacement card, run reset, database edit, or completion was performed. No code diff, test exit, tool-backed coding call, review receipt, or completion receipt exists beyond the already-recorded baseline evidence.

## Runtime repair and replacement lifecycle continuation — 2026-10-12T19:36Z

The earlier `pydantic_core._pydantic_core` failure was interpreter-routing drift, not an unverified package-version mismatch. The failed worker used image Python 3.11.17 (`/usr/local/bin/python3`) while the disposable PM environment contains only the CPython-3.14 extension. No production Python, image, or directory was changed. The repaired native launch used the disposable PM store interpreter `/rehearsal/home/tools/python-3.14.7+20260901-linux-x64/bin/python3 -I`, source mounted read-only at `/core`, and the selected disposable dependency environment. Runtime readback recorded CPython 3.14.7, `openai==2.24.0`, `pydantic==2.13.4`, `pydantic_core==2.46.4`, and successful `OpenAI(...)` client construction.

A same-launch-contract Qwen tool-capable sentinel completed with exit 0 and exact stdout `QWEN_TOOL_SENTINEL_OK`; this is real worker tool execution, not import-only evidence. Containers used `--init`, read-only root, `--cap-drop ALL`, `pids-limit=256`, `memory=2g`, `cpus=2`, candidate read-only mounting, and a persistent bounded dispatcher container for worker lifetime. No production home, credential, Docker socket, service bus, or candidate source/test path was mounted writable.

Original `rehearsal-lifecycle/t_0bf62bfd` run 1 was read back before continuation and remains unmodified. Two failed replacement histories were retained separately: `rehearsal-rpl-nbg0oi/t_09a78aa4` completed the calculator fix and tests but blocked because no board policy was active; `rehearsal-rpl2-nbg0oi/t_b5a699ba` was created only after supported dashboard configuration/board activation. Its native coder run 1 had a real claim, spawn, heartbeats, managed `review_requested` receipt, fixture-local commit `203230175068016f3787acc13d64531ccef95f19`, and summary `pytest -q tests` 3 passed with `calculator.py` only.

The second replacement's independent reviewer run 2 was **not accepted as paid independent review**: the disposable `rehearsal-reviewer` profile was configured to `custom:qwen`, and `auth list` showed only one `custom:qwen` credential. It crashed after the managed handoff due to a generic terminal-call protocol mismatch and the card blocked under `max_retries=1`. No paid reviewer credential or authorized paid profile is present in the disposable reviewer environment; Qwen cannot be substituted for the required paid review. Both dispatcher containers were stopped after their workers reached terminal state and exact native readbacks/logs were preserved under the persistent run root. This is the current genuine blocker; a later authorized continuation needs a paid reviewer profile/credential, then a fresh post-policy unclaimed replacement card and new native run. Candidate code/tests remain unchanged; this record is the sole candidate modification.

## Authorized shared-credential reset and preflight gate — 2026-10-12T17:08 EDT

Installed Hermes `auth reset` accepts an exact provider credential ID. Its implementation resolves the target by ID and calls the single-entry `reset_status(matched.id)` path; it does not use the provider-wide reset branch when `2194db` is supplied. The exact supported command `hermes auth reset openai-codex 2194db` ran once at `2026-10-12T17:08:15-04:00` after a secret-free precheck showed the one root credential as `usage_limit_reached (429)` with roughly 1d 6h remaining. Immediate root and `worker-architect-sol` readbacks both resolved the same one credential `2194db` with no cooldown marker.

The supported profile fallback is sufficient for this shared resolution: the reviewer profile has no own `auth.json` Codex entry, so it resolves the global-root credential read-only per provider. No credential was copied, re-added, removed, refreshed, or manually edited, and no production profile, board, worktree, state, or configuration was changed. Detailed secret-free command/status output is retained at `run-20261012T183249Z-Nbg0oi/logs/credential-reset-and-paid-preflight-20261012.md`.

The single intended paid preflight was explicitly pinned to `openai-codex/gpt-6.1-sol` and instructed one harmless `pwd` terminal-tool call. The launcher returned `pending_approval` before execution because its security gate could not analyze the nested command body. It therefore generated no provider request, no session ID, no tool call, and no fresh provider rejection. It was not altered or retried. Native review recovery for `rehearsal-rpl2-nbg0oi/t_b5a699ba` remains unattempted: preserved run 2 stays failed, budgets/history and candidate `203230175068016f3787acc13d64531ccef95f19` remain unchanged, and no receipt or approval was fabricated.
