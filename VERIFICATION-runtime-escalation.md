# Runtime-exhaustion escalation verification

## Scope and acceptance coverage

- Added a separate board-level `runtime_escalation` policy and dashboard API (`PUT /runtime-escalation`). It is disabled by default and requires explicit implementation/reviewer profiles.
- Kept review-correction escalation independent. Runtime escalation is eligible only for the existing exact started-worker iteration-exhaustion evidence after the implementation recovery phase budget is fully consumed.
- Persisted a task-scoped, idempotent intent before native effects with the immutable original binding, failed run, phase, checkpoint, target implementation/reviewer route, and consumed attempt.
- The watchdog resumes the same native task, readbacks native unblock and reassignment, then publishes effective routing. It does not create a replacement task/worktree or rewrite the original binding.
- Effective routing is consumed by implementation/reviewer handoff and approval paths through `trusted_routing`; the original binding remains immutable.
- Existing RM02 adoption remains narrow: one unbound, post-watermark, started-worker `gave_up` shape only. Enabling runtime escalation does not sweep historical blocked work.
- Added a strict TDD regression: first observed missing runtime API failure, then verified a two-recovery-budget exhaustion route, durable idempotency, immutable binding, dirty checkpoint capture, and refusal before exhaustion.

## Commands and results

```sh
uv run --no-project --with pytest python -m pytest \
  tests/test_watchdog.py::test_runtime_exhaustion_escalation_requires_consumed_phase_budget_and_preserves_binding \
  -q -o 'addopts='
# RED: 1 failed — AttributeError: state.set_runtime_escalation_policy was absent
# GREEN: 1 passed in 0.04s

uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx \
  python -c "import sys; sys.path.insert(0, '/home/ocadmin/.hermes/hermes-agent'); import pytest; raise SystemExit(pytest.main(['tests/test_watchdog.py', '-q', '-o', 'addopts=']))"
# 28 passed in 0.17s

uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx \
  python -c "import sys; sys.path.insert(0, '/home/ocadmin/.hermes/hermes-agent'); import pytest; raise SystemExit(pytest.main(['tests', '-q', '-o', 'addopts=']))"
# 84 passed, 23 skipped, 1 external FastAPI/httpx deprecation warning in 0.70s

hermes plugins doctor . --ci
# OK: runtime discovery, manifest parsing, import, and registration passed
# registrations: 2 tool(s), 3 hook(s)

node --check dashboard/dist/index.js
git diff --check
# passed

uv run --no-project --with build python -m build --outdir /home/ocadmin/.hermes/cache/scratch/runtime-escalation-dist-20261010T011849
# Successfully built local_first_review-1.0.0.tar.gz and local_first_review-1.0.0-py3-none-any.whl
```

## Remaining limitation

The 23 native mutation fixtures were skipped because this delegated session retains Hermes' parent-only native mutation guard. No live board, profile, installed plugin, worktree, gateway, or deployment state was touched. The parent must run those exact disposable native fixtures from an authorized non-delegated parent context to obtain the requested installed-Hermes native rehearsal evidence.
