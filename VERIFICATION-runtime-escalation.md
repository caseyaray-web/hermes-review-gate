# Runtime-exhaustion escalation verification

## Actual evidence (delegated context)

| Acceptance area | Evidence | Result |
|---|---|---|
| Durable runtime intent has exact failed run/event, checkpoint, original binding, route, and exclusive workspace lease | `tests/test_watchdog.py::test_runtime_intent_requires_exact_failure_checkpoint_and_lease` | Passed |
| Recovery budget must be exhausted before reservation; budget is not refunded | `tests/test_watchdog.py::test_runtime_exhaustion_escalation_requires_consumed_phase_budget_and_preserves_binding` | Passed |
| Pending runtime intent fences all normal worker tools | Code path added in `local_first_review.plugin.guard`; no joined native race proof in this delegated context | Unit/source coverage only |
| Reconciliation revalidates enabled policy, exact terminal event, checkpoint, immutable binding/workspace, original owner, profiles, and exclusivity before native effects | `local_first_review.plugin._reconcile_runtime_exhaustion_escalation` | Unit/source coverage only |
| Same-card exhausted recovery → native unblock/reassign → new implementation → independent review → downstream completion | `tests/test_review_gate.py::test_joined_runtime_exhaustion_escalates_same_card_after_two_recoveries` | Added; skipped by delegated native-mutation guard |
| Wrong/old profile races, lost responses, duplicate ticks, paused policy, checkpoint drift, concurrent lease, malformed events, and failed Terra no-loop behavior | No complete joined native proof yet | **Not accepted / still required** |

## Commands actually run

```sh
uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx \
  python -c "import sys; sys.path.insert(0, '/home/ocadmin/.hermes/hermes-agent'); import pytest; raise SystemExit(pytest.main(['tests/test_watchdog.py::test_runtime_intent_requires_exact_failure_checkpoint_and_lease','-q','-o','addopts=']))"
# 1 passed in 0.03s

uv run --no-project --with pytest --with ruamel.yaml --with pydantic --with fastapi --with httpx \
  python -c "import sys; sys.path.insert(0, '/home/ocadmin/.hermes/hermes-agent'); import pytest; raise SystemExit(pytest.main(['tests','-q','-o','addopts=']))"
# 85 passed, 24 skipped, 1 external FastAPI/httpx deprecation warning in 0.79s

git diff --check
# passed
```

## Blocker

This delegated session intentionally skips native mutation fixtures. The newly added joined RM03 fixture has not executed here, so it is not proof of native lifecycle behavior. Run it from a non-delegated parent with the same isolated disposable-home guard before accepting the change. The additional adversarial joined cases listed above are still absent and must be added before acceptance.
