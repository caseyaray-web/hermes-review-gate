# Deployment stabilization boundary (2026-10-11)

**Partial stabilization only. Do not deploy this branch as a v13 compatibility fix.**

Baseline: remote `2f9fbe1334243ab8844fa5b61f9df642b89001ce`; installed Hermes `46904a3b467f62616f5b3ee247adce30b1b277a0`.

## Schema origin and exact stop condition

The unpublished generalized-task-behavior branch contains the newer schema:

- `d6b97fbbfa8978f7468ff742c93198c62b7c631f` introduced v12 workflow state and `workflow_legacy_accounting=unknown`.
- `d52999ec4fedb7af37656291601cdc567efb44f8` introduced v13 prepared reviewer-verdict intents and reconciliation.
- `c00bbccc689334db80b3456d98ebd4a218d641c5` introduced v14 effect-intent hardening.
- Inspected branch HEAD: `c1264aeda2903931d22f888d7113a58d2e53412a`.

This establishes source-format origin, not which process wrote the live file. Do not attribute the production write to a specific test/process without further evidence.

A private copied production ledger has schema 13, unknown historical workflow accounting, one open workflow finding and one prepared verdict intent. The remaining workflow collections are empty. The records have fixture-like identifiers but that is not authority to delete or reclassify them. A prepared verdict is not proof the native effect happened, nor proof that no effect happened.

The current public runtime understands schema 11 only. Preserving unknown JSON keys mechanically does not make it enforce the newer finding/verdict protocol. Relabelling the file as v11 or ignoring these records would orphan their safety semantics. Porting the generalized runtime wholesale would exceed this stabilization scope.

Pure validation of an in-memory copy by the generalized branch succeeds and migrates to v14, changing only the version for this particular document; all other fields compare equal. No migrated file was saved. This proves a compatible validator exists, not that deploying that larger runtime is accepted or that its pending verdict can be reconciled safely.

**Stop:** no migration or permissive v13 loader was implemented. A separately authorized, bounded disposition of the newer pending records is required before a minimal public-runtime compatibility plan can be proved safe. Preserve original bytes and all receipts; require exact native evidence before deciding whether a prepared intent is pending, applied or irreconcilable. Do not infer authorization from fixture-looking IDs. This branch adds actionable refusal diagnostics rather than pretending compatibility.

## Independent completed work

- Document the supported new-ticket creation contract: `--workspace worktree:<absolute-repo-root>`, board-policy implementer, `--max-retries 1`, configured reviewer/escalation profiles, same installed artifact in all roles. No global failure-policy change or existing-card rewrite.
- Extend the native exhausted-worker regression to prove task `max_retries=1` wins over dispatcher `failure_limit=4`, retains the materialized worktree, and traverses the existing recovery/review path.
- Add schema-13/future-schema **read** refusal tests asserting actionable diagnostics and byte-for-byte unchanged input/no new files on `load_state()`. Mutating entry points retain the existing lock protocol: they may create a `.lock` sidecar before rejecting an unsupported ledger, but never change its bytes or enter the mutation body. A separate regression covers that boundary. Retain opaque v11 evidence across supported roundtrip. These are refusal/preservation tests, not v13 acceptance tests.
- Repair the host CLI launchers using `hermes_cli.venv_sync.publish_launchers(root, create=False)`, which uses the committed managed interpreter and repairs only existing owned PATH exposure. No PM repair/update, plugin installation, configuration modification or gateway restart.

## Verification

- Baseline: **213 passed**, no skips.
- Candidate: **218 passed**, no skips; `git diff --check` and dashboard `node --check` pass. The earlier 216-test pass preceded two additional mutation-refusal boundary cases.
- `hermes --print-runtime-command -- --version` resolves to the persistent `~/.hermes/tools/.../bin/python3`, not pytest scratch; a fresh `hermes --version` succeeds at host commit `46904a3b`.
- Launcher repair hash-checks the ledger, profile configurations, default configuration, tools facts and installed runtime Python files unchanged.
- Five fresh isolated processes loaded copied installed plugin/config/skill trees for default/dashboard, local implementer, ordinary reviewer, paid implementer and paid reviewer. Each discovered one enabled review plugin, two tools and three hooks; both tool handlers originated in the copied installation. `sdlc-review` is listed in both reviewer copies under Kanban context. No provider request or native task operation was made by these probes. They prove copied-install discovery/readiness, not current gateway in-memory code or provider credentials/quota.
- Installed runtime Python, manifest and dashboard API/bundle bytes in all five profiles match baseline remote source. The diagnostic-only candidate is intentionally not installed.
- Private copied v13 ledger SHA-256: `b7267dae03da9ddbcdf2c65e4d3a50e2895458ef98733abf804a22d27ae91d6b`. Live ledger still matched this hash at final inspection. The raw ledger is not committed.

Evidence is retained under `~/.hermes/cache/scratch/review-gate-stabilization/`: baseline/candidate JUnit, schema-investigation JSON, profile-readiness JSON, launcher-repair report and launcher backups. Scratch is not durable archival storage.

## Remaining acceptance gates

The public-plugin copied-v13-load criterion is **not met**. Operational recovery remains blocked by its schema incompatibility. Provider-backed rehearsal, actual gateway import coherence and a deployable migration are not established. Live ledger/policies/boards/worktrees/gateway remain untouched. The launcher repair is the sole production-adjacent change explicitly requested and performed. No Goal 2, context-delivery, decomposition or broader cleanup work was started.
