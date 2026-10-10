"""Plugin-owned review-gate policy, immutable bindings, and recovery ledger.

Native Kanban remains lifecycle authority.  This file records only policy scope,
immutable provenance, and recovery admission evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterator

CONFIG_NAME = "local-first-review.json"
MAX_CHANGES = 2
ESCALATION_MAX_ATTEMPTS_LIMIT = 3
# A board may grant at most this many failed-run recoveries per task phase.
# Keep this deliberately small: recovery preserves partial work but does not
# replace an operator's investigation of recurring worker failures.
RECOVERY_MAX_PER_PHASE = 1
RECOVERY_MAX_PER_PHASE_LIMIT = 5


def state_path() -> Path:
    from hermes_cli.kanban_db import kanban_home
    return kanban_home() / CONFIG_NAME


def board_name() -> str:
    return os.environ.get("HERMES_KANBAN_BOARD") or "default"


def binding_key(board: str, task_id: str) -> str:
    if not board or not task_id:
        raise ValueError("board and task_id are required")
    return f"{board}:{task_id}"


def recovery_key(board: str, task_id: str, failed_run_id: int, phase: str) -> str:
    if phase not in {"implementation", "review"} or type(failed_run_id) is not int or failed_run_id <= 0:
        raise ValueError("recovery identity is invalid")
    return f"{binding_key(board, task_id)}:{failed_run_id}:{phase}"


def phase_budget_key(board: str, task_id: str, phase: str) -> str:
    if phase not in {"implementation", "review"}:
        raise ValueError("recovery phase is invalid")
    return f"{binding_key(board, task_id)}:{phase}"


def _empty_state() -> dict[str, Any]:
    return {"version": 9, "implementation_profile": None, "reviewer_profile": None,
            "tasks": {}, "boards": {}, "recovery": {}, "recovery_budgets": {}, "workspace_leases": {},
            "escalations": {}, "runtime_escalations": {}, "effective_routing": {}}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _runtime_transport(value: Any) -> dict[str, Any] | None:
    """Validate the bounded persisted native-unblock transport diagnostic."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("operation") != "kanban_unblock":
        raise ValueError("runtime escalation transport record is invalid")
    has_result, has_error = "result" in value, "error" in value
    if has_result == has_error:
        raise ValueError("runtime escalation transport record is invalid")
    if has_result:
        if not isinstance(value["result"], dict):
            raise ValueError("runtime escalation transport record is invalid")
    else:
        error = value["error"]
        if (not isinstance(error, dict) or set(error) != {"type", "message"}
                or not _text(error.get("type")) or not _text(error.get("message"))):
            raise ValueError("runtime escalation transport record is invalid")
    try:
        encoded = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime escalation transport record is invalid") from exc
    if len(encoded.encode()) > 4096:
        raise ValueError("runtime escalation transport record is invalid")
    return json.loads(encoded)


def _recovery_settings(value: Any) -> dict[str, Any]:
    """Validate the persisted board policy; zero is never a pause surrogate."""
    if not isinstance(value, dict) or set(value) != {"enabled", "max_per_phase"}:
        raise ValueError("review recovery policy is invalid")
    maximum = value.get("max_per_phase")
    if (type(value.get("enabled")) is not bool or type(maximum) is not int
            or not 1 <= maximum <= RECOVERY_MAX_PER_PHASE_LIMIT):
        raise ValueError("review recovery policy is invalid")
    return {"enabled": value["enabled"], "max_per_phase": maximum}


def _escalation_settings(value: Any) -> dict[str, Any]:
    """Validate opt-in substantive-review escalation independently of recovery."""
    fields = {"enabled", "normal_correction_limit", "max_attempts", "implementation_profile", "reviewer_profile"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("review escalation policy is invalid")
    enabled, limit, attempts = value["enabled"], value["normal_correction_limit"], value["max_attempts"]
    implementation, reviewer = value["implementation_profile"], value["reviewer_profile"]
    if (type(enabled) is not bool or type(limit) is not int or not 1 <= limit <= MAX_CHANGES
            or type(attempts) is not int or not 1 <= attempts <= ESCALATION_MAX_ATTEMPTS_LIMIT):
        raise ValueError("review escalation policy is invalid")
    if enabled and (not _text(implementation) or not _text(reviewer)):
        raise ValueError("enabled review escalation requires implementation and reviewer profiles")
    if not enabled and (implementation is not None or reviewer is not None):
        raise ValueError("disabled review escalation must not retain routing")
    return dict(value)


def _runtime_escalation_settings(value: Any) -> dict[str, Any]:
    """Validate the distinct, explicit runtime-exhaustion escalation policy."""
    fields = {"enabled", "max_attempts", "implementation_profile", "reviewer_profile"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("runtime escalation policy is invalid")
    enabled, attempts = value["enabled"], value["max_attempts"]
    implementation, reviewer = value["implementation_profile"], value["reviewer_profile"]
    # Runtime exhaustion has one replacement route per failed-run lineage;
    # accepting a larger number falsely advertises retries that do not exist.
    if type(enabled) is not bool or type(attempts) is not int or attempts != 1:
        raise ValueError("runtime escalation policy is invalid")
    if enabled and (not _text(implementation) or not _text(reviewer) or implementation == reviewer):
        raise ValueError("enabled runtime escalation requires distinct implementation and reviewer profiles")
    if not enabled and (implementation is not None or reviewer is not None):
        raise ValueError("disabled runtime escalation must not retain routing")
    return dict(value)


def _migrate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        raise ValueError("review-gate configuration has an invalid shape")
    if data.get("version", 1) == 1:
        data = dict(data); data["version"] = 2; data["boards"] = {}
    if data.get("version") == 2:
        data = dict(data); data["version"] = 3; data["boards"] = {}
    if data.get("version") == 3:
        # v3 has no recovery authority.  Deliberately create no adoption
        # records: only a fresh, exact post-watermark native observation can.
        data = dict(data); data["version"] = 4; data["recovery"] = {}
        data["recovery_budgets"] = {}; data["workspace_leases"] = {}
    if data.get("version") == 4:
        data = dict(data); data["version"] = 5; data["escalations"] = {}; data["effective_routing"] = {}
    if data.get("version") == 5:
        data = dict(data); data["version"] = 6
        data["escalations"] = {
            key: ({"board": entry.get("board"), "task_id": entry.get("task_id"), "binding": entry.get("binding"),
                   "consumed_attempts": entry.get("attempt"), "current_attempt": entry.get("attempt"),
                   "unavailable_prior_attempts": entry["attempt"] - 1 if type(entry.get("attempt")) is int else -1,
                   "attempts": [{field: entry.get(field) for field in ("attempt", "implementation_profile", "reviewer_profile", "intent")}]} if isinstance(entry, dict) else entry)
            for key, entry in data.get("escalations", {}).items()
        }
    if data.get("version") == 6:
        data = dict(data); data["version"] = 7; data["runtime_escalations"] = {}
    if data.get("version") == 7:
        # v7 runtime intents lacked an exact terminal-event receipt and lease.
        # Preserve them for audit, but reconciliation must never resume them.
        data = dict(data); data["version"] = 8
        for entry in data.get("runtime_escalations", {}).values():
            if isinstance(entry, dict):
                entry.setdefault("legacy_unverifiable", True)
                binding = entry.get("binding")
                if isinstance(binding, dict):
                    entry.setdefault("workspace_path", binding.get("workspace_path"))
    if data.get("version") == 8:
        # v8 intents predate explicit catch-up context. They remain auditable,
        # but only a new task-scoped adoption carries a fresh coder packet.
        data = dict(data); data["version"] = 9
    return data


def _validate(data: Any) -> dict[str, Any]:
    data = _migrate(data)
    required_roots = ("boards", "recovery", "recovery_budgets", "workspace_leases", "escalations", "runtime_escalations", "effective_routing")
    if data.get("version") != 9 or any(not isinstance(data.get(k), dict) for k in required_roots):
        raise ValueError("review-gate configuration has an invalid shape")
    for key, binding in data["tasks"].items():
        if not isinstance(key, str) or not isinstance(binding, dict):
            raise ValueError("review-gate enrollment has an invalid shape")
        fields = ("board", "task_id", "implementation_profile", "reviewer_profile", "workspace_path")
        if any(not _text(binding.get(field)) for field in fields) or key != binding_key(binding["board"], binding["task_id"]):
            raise ValueError("review-gate enrollment is incomplete")
        policy_fields = ("policy_activation_id", "policy_native_run_watermark", "native_run_id")
        present = [field in binding for field in policy_fields]
        if any(present) and not all(present):
            raise ValueError("review-gate policy binding provenance is incomplete")
        if all(present) and (not _text(binding["policy_activation_id"])
                                or type(binding["policy_native_run_watermark"]) is not int
                                or binding["policy_native_run_watermark"] < 0
                                or type(binding["native_run_id"]) is not int or binding["native_run_id"] <= 0):
            raise ValueError("review-gate policy binding provenance is invalid")
    for board, policy in data["boards"].items():
        if not _text(board) or not isinstance(policy, dict):
            raise ValueError("review-gate board policy has an invalid shape")
        fields = ("activation_id", "implementation_profile", "reviewer_profile")
        if (any(not _text(policy.get(x)) for x in fields) or type(policy.get("native_run_watermark")) is not int
                or policy["native_run_watermark"] < 0):
            raise ValueError("review-gate board policy is invalid")
        _recovery_settings(policy.get("recovery", {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}))
        _escalation_settings(policy.get("escalation", {"enabled": False, "normal_correction_limit": MAX_CHANGES,
                                                        "max_attempts": 1, "implementation_profile": None,
                                                        "reviewer_profile": None}))
        _runtime_escalation_settings(policy.get("runtime_escalation", {"enabled": False, "max_attempts": 1,
                                                                          "implementation_profile": None,
                                                                          "reviewer_profile": None}))
    for key, entry in data["escalations"].items():
        if (not isinstance(key, str) or not isinstance(entry, dict) or not _text(entry.get("board"))
                or not _text(entry.get("task_id")) or key != binding_key(entry["board"], entry["task_id"])):
            raise ValueError("review escalation ledger is invalid")
        missing = entry.get("unavailable_prior_attempts", 0)
        if (type(missing) is not int or missing < 0
                or not isinstance(entry.get("binding"), dict) or type(entry.get("consumed_attempts")) is not int
                or type(entry.get("current_attempt")) is not int or not isinstance(entry.get("attempts"), list)
                or not entry["attempts"] or entry["consumed_attempts"] != missing + len(entry["attempts"])
                or entry["current_attempt"] != entry["consumed_attempts"]):
            raise ValueError("review escalation ledger is invalid")
        for number, attempt in enumerate(entry["attempts"], start=missing + 1):
            if (not isinstance(attempt, dict) or attempt.get("attempt") != number or not isinstance(attempt.get("intent"), dict)
                    or not _text(attempt.get("implementation_profile")) or not _text(attempt.get("reviewer_profile"))):
                raise ValueError("review escalation ledger is invalid")
    for key, entry in data["runtime_escalations"].items():
        if (not isinstance(key, str) or not isinstance(entry, dict) or not _text(entry.get("board"))
                or not _text(entry.get("task_id")) or key != binding_key(entry["board"], entry["task_id"])
                or not isinstance(entry.get("binding"), dict) or type(entry.get("failed_run_id")) is not int
                or entry["failed_run_id"] <= 0 or entry.get("phase") != "implementation"
                or not _text(entry.get("workspace_path"))
                or type(entry.get("consumed_attempts")) is not int or not isinstance(entry.get("attempts"), list)
                or entry["consumed_attempts"] != len(entry["attempts"])):
            raise ValueError("runtime escalation ledger is invalid")
        if not entry.get("legacy_unverifiable"):
            failure, checkpoint = entry.get("failure"), entry.get("checkpoint")
            if (not isinstance(failure, dict) or failure.get("run_id") != entry["failed_run_id"]
                    or type(failure.get("event_id")) is not int or failure["event_id"] <= 0
                    or failure.get("kind") != "gave_up" or not isinstance(failure.get("payload"), dict)
                    or not isinstance(checkpoint, dict)):
                raise ValueError("runtime escalation evidence is invalid")
        context = entry.get("coder_context")
        if context is not None:
            if entry.get("legacy_unverifiable"):
                raise ValueError("legacy runtime escalation cannot carry coder context")
            _runtime_coder_context(context, failure=entry["failure"], checkpoint=entry["checkpoint"])
        entry_intent = entry.get("intent")
        if not isinstance(entry_intent, dict):
            raise ValueError("runtime escalation ledger is invalid")
        history = entry_intent.get("held_reason_history")
        if history is not None and (not isinstance(history, list) or any(not _text(reason) for reason in history)):
            raise ValueError("runtime held reason history is invalid")
        no_effect = entry_intent.get("operator_no_effect_reconciliation")
        if no_effect is not None:
            required = {"binding", "failure", "checkpoint", "failed_run_id", "snapshot", "snapshot_sha256"}
            if not isinstance(no_effect, dict) or set(no_effect) != required:
                raise ValueError("runtime held reconciliation evidence is invalid")
            try:
                snapshot_bytes = json.dumps(no_effect["snapshot"], sort_keys=True, separators=(",", ":")).encode()
                import hashlib
                digest = hashlib.sha256(snapshot_bytes).hexdigest()
            except (TypeError, ValueError) as exc:
                raise ValueError("runtime held reconciliation evidence is invalid") from exc
            if (len(snapshot_bytes) > 524288 or no_effect["binding"] != entry["binding"]
                    or no_effect["failure"] != entry.get("failure") or no_effect["checkpoint"] != entry.get("checkpoint")
                    or no_effect["failed_run_id"] != entry["failed_run_id"] or no_effect["snapshot_sha256"] != digest
                    or not isinstance(no_effect["snapshot"], dict)
                    or set(no_effect["snapshot"]) != {"task", "runs", "events"}):
                raise ValueError("runtime held reconciliation evidence is invalid")
        transport = _runtime_transport(entry_intent.get("transport"))
        continuation_history = entry_intent.get("operator_continuation_history")
        if continuation_history is not None:
            if (not isinstance(continuation_history, list) or len(continuation_history) != 1
                    or not isinstance(continuation_history[0], dict)
                    or set(continuation_history[0]) != {"receipt", "snapshot"}
                    or not isinstance(continuation_history[0]["receipt"], dict)
                    or not isinstance(continuation_history[0]["snapshot"], dict)
                    or set(continuation_history[0]["snapshot"]) != {"task", "runs", "events"}):
                raise ValueError("runtime operator continuation history is invalid")
            try:
                continuation_bytes = json.dumps(continuation_history, sort_keys=True, separators=(",", ":")).encode()
            except (TypeError, ValueError) as exc:
                raise ValueError("runtime operator continuation history is invalid") from exc
            if len(continuation_bytes) > 524288:
                raise ValueError("runtime operator continuation history is invalid")
        transport_history = entry_intent.get("transport_history")
        if transport_history is not None:
            if (not isinstance(transport_history, list) or any(_runtime_transport(item) is None for item in transport_history)
                    or (transport is not None and (not transport_history or transport_history[0] != transport))):
                raise ValueError("runtime escalation transport history is invalid")
        for number, attempt in enumerate(entry["attempts"], start=1):
            if (not isinstance(attempt, dict) or attempt.get("attempt") != number
                    or not _text(attempt.get("implementation_profile")) or not _text(attempt.get("reviewer_profile"))
                    or not isinstance(attempt.get("intent"), dict)):
                raise ValueError("runtime escalation ledger is invalid")
            if _runtime_transport(attempt["intent"].get("transport")) != transport:
                raise ValueError("runtime escalation transport record is invalid")
    for key, route in data["effective_routing"].items():
        if (not isinstance(key, str) or not isinstance(route, dict) or not _text(route.get("board"))
                or not _text(route.get("task_id")) or key != binding_key(route["board"], route["task_id"])):
            raise ValueError("effective review routing is invalid")
        if not _text(route.get("implementation_profile")) or not _text(route.get("reviewer_profile")):
            raise ValueError("effective review routing is invalid")
    for key, entry in data["recovery"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError("recovery ledger is invalid")
        fields = ("board", "task_id", "phase", "workspace_path", "checkpoint", "intent", "failed_run_id")
        if any(field not in entry for field in fields) or key != recovery_key(entry["board"], entry["task_id"], entry["failed_run_id"], entry["phase"]):
            raise ValueError("recovery identity is invalid")
    return data


def _read_unlocked() -> dict[str, Any]:
    path = state_path()
    if not path.exists(): return _empty_state()
    try: return _validate(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"review-gate configuration is unreadable: {exc}") from exc


def _write_unlocked(data: dict[str, Any]) -> None:
    data = _validate(data); path = state_path(); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try: temp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n"); temp.replace(path)
    finally: temp.unlink(missing_ok=True)


@contextmanager
def locked_state(*, write: bool = False) -> Iterator[dict[str, Any]]:
    path = state_path(); path.parent.mkdir(parents=True, exist_ok=True); lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            data = _read_unlocked(); yield data
            if write: _write_unlocked(data)
        finally: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_state() -> dict[str, Any]: return _read_unlocked()

def save_state(data: dict[str, Any]) -> None:
    _validate(data); replacement = json.loads(json.dumps(data))
    with locked_state(write=True) as stored: stored.clear(); stored.update(replacement)

def task_binding(task_id: str, board: str | None = None) -> dict[str, Any] | None:
    return load_state()["tasks"].get(binding_key(board or board_name(), task_id))

def board_policy(board: str) -> dict[str, Any] | None: return load_state()["boards"].get(board)


@contextmanager
def runtime_escalation_operation() -> Iterator[bool]:
    """Try to join the native runtime-effect authority without deadlocking it.

    Supported runtime control writes use this same non-reentrant lock as an
    effect reconciliation.  A writer invoked by an effect seam therefore fails
    closed instead of waiting on itself while native authority is pinned.
    """
    path = state_path().with_suffix('.runtime.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)

def set_recovery_policy(board: str, *, enabled: bool, max_per_phase: int | None = None) -> dict[str, Any]:
    with locked_state(write=True) as data:
        policy = data["boards"].get(board)
        if policy is None: raise ValueError("board has no active review-gate policy")
        current = _recovery_settings(policy.get("recovery", {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}))
        if type(enabled) is not bool:
            raise ValueError("recovery enabled must be a boolean")
        maximum = current["max_per_phase"] if max_per_phase is None else max_per_phase
        policy["recovery"] = _recovery_settings({"enabled": enabled, "max_per_phase": maximum})
        return json.loads(json.dumps(policy))


def set_escalation_policy(board: str, *, enabled: bool, normal_correction_limit: int | None = None,
                          max_attempts: int | None = None, implementation_profile: str | None = None,
                          reviewer_profile: str | None = None) -> dict[str, Any]:
    with locked_state(write=True) as data:
        policy = data["boards"].get(board)
        if policy is None:
            raise ValueError("board has no active review-gate policy")
        current = _escalation_settings(policy.get("escalation", {"enabled": False, "normal_correction_limit": MAX_CHANGES,
                                                                    "max_attempts": 1, "implementation_profile": None,
                                                                    "reviewer_profile": None}))
        proposed = {"enabled": enabled, "normal_correction_limit": current["normal_correction_limit"] if normal_correction_limit is None else normal_correction_limit,
                    "max_attempts": current["max_attempts"] if max_attempts is None else max_attempts,
                    "implementation_profile": implementation_profile if enabled else None,
                    "reviewer_profile": reviewer_profile if enabled else None}
        if enabled and (not profile_exists(implementation_profile or "") or not profile_exists(reviewer_profile or "")):
            raise ValueError("configured escalation profile is missing")
        policy["escalation"] = _escalation_settings(proposed)
        return json.loads(json.dumps(policy))


def set_runtime_escalation_policy(board: str, *, enabled: bool, max_attempts: int | None = None,
                                  implementation_profile: str | None = None,
                                  reviewer_profile: str | None = None) -> dict[str, Any]:
    """Configure an opt-in route used only after runtime recovery is exhausted."""
    with runtime_escalation_operation() as acquired:
        if not acquired:
            raise RuntimeError("runtime escalation operation is busy")
        with locked_state(write=True) as data:
            policy = data["boards"].get(board)
            if policy is None:
                raise ValueError("board has no active review-gate policy")
            current = _runtime_escalation_settings(policy.get("runtime_escalation", {"enabled": False,
                                                                                         "max_attempts": 1,
                                                                                         "implementation_profile": None,
                                                                                         "reviewer_profile": None}))
            if enabled:
                configured = policy.get("escalation", {})
                implementation_profile = implementation_profile or configured.get("implementation_profile")
                reviewer_profile = reviewer_profile or configured.get("reviewer_profile")
            proposed = {"enabled": enabled, "max_attempts": current["max_attempts"] if max_attempts is None else max_attempts,
                        "implementation_profile": implementation_profile if enabled else None,
                        "reviewer_profile": reviewer_profile if enabled else None}
            if enabled and (not profile_exists(implementation_profile or "") or not profile_exists(reviewer_profile or "")):
                raise ValueError("configured runtime escalation profile is missing")
            validated = _runtime_escalation_settings(proposed)
            if enabled and not current["enabled"]:
                from .native import board_run_watermark
                policy["runtime_escalation_watermark"] = board_run_watermark(board)
            policy["runtime_escalation"] = validated
            return json.loads(json.dumps(policy))


def runtime_escalation_entry(task_id: str, board: str | None = None) -> dict[str, Any] | None:
    return load_state()["runtime_escalations"].get(binding_key(board or board_name(), task_id))


def _runtime_coder_context(value: Any, *, failure: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Validate the bounded packet delivered to a fresh escalated coder run."""
    fields = {"original_contract", "reviewer_findings", "latest_failure", "checkpoint"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("runtime coder context is invalid")
    contract, findings = value.get("original_contract"), value.get("reviewer_findings")
    if (not isinstance(contract, dict) or not contract or not isinstance(findings, list)
            or value.get("latest_failure") != failure or value.get("checkpoint") != checkpoint):
        raise ValueError("runtime coder context is incomplete")
    try:
        encoded = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime coder context is invalid") from exc
    if len(encoded.encode()) > 65536 or len(findings) > 32:
        raise ValueError("runtime coder context exceeds its bounded limit")
    for finding in findings:
        if not isinstance(finding, dict) or type(finding.get("run_id")) is not int or finding["run_id"] <= 0:
            raise ValueError("runtime coder context reviewer findings are invalid")
    return json.loads(encoded)


def reserve_runtime_escalation(board: str, task_id: str, *, failed_run_id: int, phase: str,
                                binding: dict[str, Any], checkpoint: dict[str, Any], failure: dict[str, Any],
                                workspace_path: str, coder_context: dict[str, Any] | None = None,
                                catchup: bool = False) -> dict[str, Any]:
    """Reserve one auditable replacement route before any native lifecycle effect."""
    if phase != "implementation" or type(failed_run_id) is not int or failed_run_id <= 0:
        raise ValueError("runtime escalation identity is invalid")
    try:
        same_workspace = (isinstance(workspace_path, str) and isinstance(binding.get("workspace_path"), str)
                          and Path(workspace_path).resolve() == Path(binding["workspace_path"]).resolve())
    except (OSError, RuntimeError):
        same_workspace = False
    if (not isinstance(failure, dict) or failure.get("run_id") != failed_run_id
            or type(failure.get("event_id")) is not int or failure["event_id"] <= 0
            or failure.get("kind") != "gave_up" or not isinstance(failure.get("payload"), dict)
            or not isinstance(checkpoint, dict) or not same_workspace):
        raise ValueError("runtime escalation evidence is invalid")
    if catchup and coder_context is None:
        raise ValueError("explicit runtime catch-up requires coder context")
    if coder_context is not None:
        coder_context = _runtime_coder_context(coder_context, failure=failure, checkpoint=checkpoint)
    key, budget = binding_key(board, task_id), phase_budget_key(board, task_id, phase)
    lease_key = f"{board}:{task_id}:{failed_run_id}:runtime_escalation"
    with locked_state(write=True) as data:
        policy = data["boards"].get(board)
        if policy is None:
            raise ValueError("runtime escalation requires an active board policy")
        settings = _runtime_escalation_settings(policy.get("runtime_escalation", {"enabled": False, "max_attempts": 1,
                                                                                     "implementation_profile": None, "reviewer_profile": None}))
        if not settings["enabled"]:
            raise ValueError("runtime escalation is disabled")
        existing = data["runtime_escalations"].get(key)
        if existing is not None:
            if (existing.get("binding") != binding or existing.get("failed_run_id") != failed_run_id
                    or existing.get("failure") != failure or existing.get("checkpoint") != checkpoint
                    or (coder_context is not None and existing.get("coder_context") != coder_context)):
                raise ValueError("runtime escalation identity conflicts with an existing intent")
            return json.loads(json.dumps(existing))
        if data['tasks'].get(key) != binding:
            raise ValueError('runtime escalation original binding is missing or changed')
        if key in data['effective_routing'] or key in data['escalations']:
            raise ValueError('configured escalation attempt has already been reserved')
        watermark = policy.get("runtime_escalation_watermark")
        if not catchup and (type(watermark) is not int or failed_run_id <= watermark):
            raise ValueError("historical runtime failure requires explicit task-scoped catch-up")
        recovery = _recovery_settings(policy.get("recovery", {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}))
        if data["recovery_budgets"].get(budget, 0) < recovery["max_per_phase"]:
            raise ValueError("runtime recovery budget is not exhausted")
        if not profile_exists(settings["implementation_profile"]) or not profile_exists(settings["reviewer_profile"]):
            raise ValueError("configured runtime escalation profile is missing")
        holder = data["workspace_leases"].get(workspace_path)
        if holder is not None and holder != lease_key:
            raise ValueError("workspace escalation lease is held by another task")
        attempt = {"attempt": 1, "implementation_profile": settings["implementation_profile"],
                   "reviewer_profile": settings["reviewer_profile"],
                   "intent": {"status": "catchup_requested" if catchup else "unblock_requested", "checkpoint": dict(checkpoint)}}
        entry = {"board": board, "task_id": task_id, "binding": dict(binding), "failed_run_id": failed_run_id,
                 "phase": phase, "workspace_path": workspace_path, "failure": dict(failure), "checkpoint": dict(checkpoint),
                 "consumed_attempts": 1, "intent": dict(attempt["intent"]), "attempts": [attempt]}
        if coder_context is not None:
            entry["coder_context"] = coder_context
        data["runtime_escalations"][key] = entry
        data["workspace_leases"][workspace_path] = lease_key
        return json.loads(json.dumps(entry))


def adopt_runtime_escalation(board: str, task_id: str, **evidence: Any) -> dict[str, Any]:
    """Persist one operator-selected historical RM03 catch-up intent only."""
    return reserve_runtime_escalation(board, task_id, catchup=True, **evidence)


def update_runtime_escalation_intent(board: str, task_id: str, status: str, *, reason: str | None = None) -> dict[str, Any]:
    if status not in {"unblock_requested", "unblock_attempted", "unblock_verified", "reassign_attempted", "routed", "held"}:
        raise ValueError("runtime escalation intent status is invalid")
    with locked_state(write=True) as data:
        entry = data["runtime_escalations"].get(binding_key(board, task_id))
        if not isinstance(entry, dict):
            raise ValueError("runtime escalation intent is absent")
        entry["intent"] = dict(entry.get("intent", {}), status=status)
        if reason is not None:
            entry["intent"]["reason"] = reason
        attempts = entry.get("attempts")
        if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict):
            attempts[-1]["intent"] = dict(attempts[-1].get("intent", {}), status=status)
        return json.loads(json.dumps(entry))


def authorize_runtime_held_reconciliation(board: str, task_id: str, *, entry: dict[str, Any],
                                           evidence: dict[str, Any]) -> dict[str, Any]:
    """Persist operator no-effect proof and reopen exactly the reserved effect.

    The caller supplies a complete read-only native snapshot while holding the
    runtime operation lock.  This write intentionally creates no transport
    receipt: no native effect has occurred yet.
    """
    required = {'binding', 'failure', 'checkpoint', 'failed_run_id', 'snapshot', 'snapshot_sha256'}
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise ValueError('runtime held reconciliation evidence is invalid')
    try:
        snapshot_bytes = json.dumps(evidence['snapshot'], sort_keys=True, separators=(',', ':')).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError('runtime held reconciliation evidence is invalid') from exc
    import hashlib
    if (len(snapshot_bytes) > 524288 or not isinstance(evidence['snapshot_sha256'], str)
            or hashlib.sha256(snapshot_bytes).hexdigest() != evidence['snapshot_sha256']):
        raise ValueError('runtime held reconciliation evidence is invalid')
    with locked_state(write=True) as data:
        current = data['runtime_escalations'].get(binding_key(board, task_id))
        if (current != entry or current.get('intent', {}).get('status') != 'held'
                or 'transport' in current.get('intent', {})):
            raise ValueError('runtime held reconciliation intent changed')
        if (evidence['binding'] != current.get('binding') or evidence['failure'] != current.get('failure')
                or evidence['checkpoint'] != current.get('checkpoint')
                or evidence['failed_run_id'] != current.get('failed_run_id')):
            raise ValueError('runtime held reconciliation evidence does not match intent')
        intent = dict(current['intent'])
        history = list(intent.get('held_reason_history', []))
        prior_reason = intent.get('reason')
        if isinstance(prior_reason, str) and prior_reason and (not history or history[-1] != prior_reason):
            history.append(prior_reason)
        intent.update(status='unblock_requested', held_reason_history=history,
                      operator_no_effect_reconciliation=json.loads(json.dumps(evidence)))
        current['intent'] = intent
        attempt = current['attempts'][-1]
        attempt['intent'] = dict(attempt['intent'], status='unblock_requested')
        return json.loads(json.dumps(current))


def authorize_operator_continuation(board: str, task_id: str, *, entry: dict[str, Any],
                                    receipt: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist the one reviewed continuation before its reserved native effect."""
    with locked_state(write=True) as data:
        current = data['runtime_escalations'].get(binding_key(board, task_id))
        if (current != entry or current.get('intent', {}).get('status') != 'held'
                or current.get('intent', {}).get('operator_continuation_history')):
            raise ValueError('runtime operator continuation intent changed')
        intent = dict(current['intent'])
        intent.update(status='unblock_requested', operator_continuation_history=[{
            'receipt': json.loads(json.dumps(receipt)), 'snapshot': json.loads(json.dumps(snapshot)),
        }])
        current['intent'] = intent
        current['attempts'][-1]['intent'] = dict(current['attempts'][-1]['intent'], status='unblock_requested')
        return json.loads(json.dumps(current))


def record_runtime_escalation_transport(board: str, task_id: str, operation: str, *,
                                        result: dict[str, Any] | None = None,
                                        error: BaseException | None = None) -> dict[str, Any]:
    """Persist the bounded outcome of one native transport call before readback."""
    if operation != "kanban_unblock" or (result is None) == (error is None):
        raise ValueError("runtime escalation transport record is invalid")
    if result is not None:
        try:
            encoded = json.dumps(result, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime escalation transport result is invalid") from exc
        if len(encoded.encode()) > 4096:
            raise ValueError("runtime escalation transport result exceeds its bounded limit")
        transport = {"operation": operation, "result": json.loads(encoded)}
    else:
        message = str(error)
        if len(message.encode()) > 2048:
            message = message.encode()[:2048].decode(errors="replace")
        transport = {"operation": operation,
                     "error": {"type": type(error).__name__, "message": message}}
    with locked_state(write=True) as data:
        entry = data["runtime_escalations"].get(binding_key(board, task_id))
        if not isinstance(entry, dict):
            raise ValueError("runtime escalation intent is absent")
        intent = dict(entry.get("intent", {}))
        history = list(intent.get("transport_history", []))
        if not history:
            history.append(intent.get("transport", transport))
            if intent.get("transport") is not None:
                history.append(transport)
        else:
            history.append(transport)
        intent["transport_history"] = history
        # Preserve the first actual transport receipt verbatim; subsequent
        # transport outcomes are append-only history.
        intent.setdefault("transport", transport)
        entry["intent"] = intent
        attempts = entry.get("attempts")
        if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict):
            attempt_intent = dict(attempts[-1].get("intent", {}))
            attempt_intent.setdefault("transport", transport)
            attempts[-1]["intent"] = attempt_intent
        return json.loads(json.dumps(entry))


def deliver_runtime_coder_context(board: str, task_id: str, run_id: int) -> dict[str, Any] | None:
    """Return a catch-up packet once for the exact fresh native coder run."""
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("runtime coder context run identity is invalid")
    with locked_state(write=True) as data:
        entry = data["runtime_escalations"].get(binding_key(board, task_id))
        if not isinstance(entry, dict) or not isinstance(entry.get("coder_context"), dict):
            return None
        delivered = entry.get("coder_context_delivery_run_id")
        if delivered is None:
            entry["coder_context_delivery_run_id"] = run_id
            return json.loads(json.dumps(entry["coder_context"]))
        if delivered != run_id:
            raise ValueError("runtime coder context was already delivered to another run")
        return None


def recovery_budget_exhausted(board: str, task_id: str, phase: str) -> bool:
    policy = board_policy(board)
    if policy is None:
        return False
    maximum = _recovery_settings(policy.get("recovery", {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}))["max_per_phase"]
    return load_state()["recovery_budgets"].get(phase_budget_key(board, task_id, phase), 0) >= maximum


def publish_runtime_escalation_routing(board: str, task_id: str, *, binding: dict[str, Any]) -> dict[str, Any]:
    """Publish routing only after the caller has read back native unblock/reassign."""
    key = binding_key(board, task_id)
    with locked_state(write=True) as data:
        entry = data["runtime_escalations"].get(key)
        if not isinstance(entry, dict) or entry.get("binding") != binding:
            raise ValueError("runtime escalation intent is absent or mismatched")
        attempt = entry["attempts"][-1]
        route = {"board": board, "task_id": task_id, "attempt": attempt["attempt"],
                 "implementation_profile": attempt["implementation_profile"], "reviewer_profile": attempt["reviewer_profile"]}
        existing = data["effective_routing"].get(key)
        if existing is not None and existing != route:
            raise ValueError("runtime escalation routing conflicts with an existing effective route")
        data["effective_routing"][key] = route
        entry["intent"] = dict(entry["intent"], status="routed")
        attempt["intent"] = dict(attempt["intent"], status="routed")
        return json.loads(json.dumps(route))


def effective_routing(task_id: str, board: str | None = None) -> dict[str, Any] | None:
    return load_state()["effective_routing"].get(binding_key(board or board_name(), task_id))


def escalation_entry(task_id: str, board: str | None = None) -> dict[str, Any] | None:
    """Return the durable escalation intent without inferring native progress."""
    return load_state()["escalations"].get(binding_key(board or board_name(), task_id))


def current_escalation_attempt(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the selected immutable attempt; callers never infer list order."""
    selector, attempts = entry.get("current_attempt"), entry.get("attempts")
    if type(selector) is not int or not isinstance(attempts, list):
        raise ValueError("review escalation current attempt is invalid")
    matches = [attempt for attempt in attempts if isinstance(attempt, dict) and attempt.get("attempt") == selector]
    if len(matches) != 1:
        raise ValueError("review escalation current attempt is invalid")
    return matches[0]


def trusted_routing(task_id: str, board: str, binding: dict[str, Any], *, include_pending: bool = False) -> dict[str, Any]:
    """Resolve only routing sealed to this immutable enrollment.

    Handoff metadata is worker-authored evidence, not routing authority. A route
    is trusted only when the ledger retained the exact original binding and its
    published effective record agrees with that intent. Pending intent is for
    admission guards only; it never pretends reassignment landed.
    """
    data = load_state(); key = binding_key(board, task_id)
    runtime = data.get("runtime_escalations", {}).get(key)
    if isinstance(runtime, dict) and runtime.get("binding") == binding and runtime.get("intent", {}).get("status") == "routed":
        attempts = runtime.get("attempts")
        attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
        route = {"board": board, "task_id": task_id,
                 "implementation_profile": attempt.get("implementation_profile") if isinstance(attempt, dict) else None,
                 "reviewer_profile": attempt.get("reviewer_profile") if isinstance(attempt, dict) else None}
        published = data["effective_routing"].get(key)
        if not isinstance(published, dict) or any(published.get(field) != route[field] for field in route):
            raise ValueError("published runtime escalation routing is inconsistent")
        return route
    entry = data.get("escalations", {}).get(key)
    if not isinstance(entry, dict) or entry.get("binding") != binding:
        return dict(binding)
    attempt = current_escalation_attempt(entry)
    intent = attempt.get("intent")
    if not isinstance(intent, dict):
        return dict(binding)
    route = {"board": board, "task_id": task_id,
             "implementation_profile": attempt.get("implementation_profile"), "reviewer_profile": attempt.get("reviewer_profile")}
    if (route["board"] != board or route["task_id"] != task_id
            or not _text(route["implementation_profile"]) or not _text(route["reviewer_profile"])):
        raise ValueError("review escalation routing is invalid")
    if intent.get("status") == "routed":
        published = data["effective_routing"].get(key)
        if not isinstance(published, dict) or any(published.get(field) != route[field] for field in route):
            raise ValueError("published escalation routing is inconsistent")
        return route
    if include_pending and intent.get("status") == "changes_requested_pending":
        return route
    return dict(binding)


def reserve_escalation(board: str, task_id: str, *, review_run_id: int, binding: dict[str, Any],
                       candidate: dict[str, Any], change_count: int) -> dict[str, Any]:
    """Persist one exact escalation routing intent before native transitions."""
    key = binding_key(board, task_id)
    with locked_state(write=True) as data:
        policy = data["boards"].get(board)
        if policy is None:
            raise ValueError("review escalation requires an active board policy")
        settings = _escalation_settings(policy.get("escalation", {"enabled": False, "normal_correction_limit": MAX_CHANGES,
                                                                     "max_attempts": 1, "implementation_profile": None,
                                                                     "reviewer_profile": None}))
        if not settings["enabled"]:
            raise ValueError("review escalation is disabled")
        existing = data["escalations"].get(key)
        if existing and any(attempt.get("intent", {}).get("review_run_id") == review_run_id
                            for attempt in existing.get("attempts", [])):
            return json.loads(json.dumps(existing))
        prior_attempt = existing.get("consumed_attempts", 0) if existing else 0
        if prior_attempt >= settings["max_attempts"]:
            raise ValueError("review escalation attempts exhausted; operator action is required")
        if change_count < settings["normal_correction_limit"]:
            raise ValueError("normal review correction budget is not exhausted")
        prior_route = data["effective_routing"].get(key)
        origin = dict(binding) if prior_route is None else {**binding,
            "implementation_profile": prior_route.get("implementation_profile"),
            "reviewer_profile": prior_route.get("reviewer_profile")}
        if not _text(origin.get("implementation_profile")) or not _text(origin.get("reviewer_profile")):
            raise ValueError("review escalation origin routing is invalid")
        attempt = {"attempt": prior_attempt + 1, "implementation_profile": settings["implementation_profile"],
                   "reviewer_profile": settings["reviewer_profile"], "origin": origin,
                   "intent": {"status": "changes_requested_pending", "review_run_id": review_run_id,
                              "candidate": dict(candidate), "change_count": change_count}}
        entry = ({"board": board, "task_id": task_id, "binding": dict(binding), "consumed_attempts": 1,
                  "current_attempt": 1, "attempts": [attempt]} if existing is None else
                 {**existing, "consumed_attempts": prior_attempt + 1, "current_attempt": prior_attempt + 1,
                  "attempts": [*existing["attempts"], attempt]})
        data["escalations"][key] = entry
        return json.loads(json.dumps(entry))


def reconcile_pending_escalation(board: str, task_id: str, *, binding: dict[str, Any], review_run_id: int,
                                 reconcile: Any) -> str:
    """Run one native read/reassign reconciliation under escalation policy lock.

    Disabling escalation preserves a pending intent and its consumed attempt, but
    holds it before any native effect or route publication.  A route already
    published is deliberately not revoked: it is an active native lifecycle.
    """
    key = binding_key(board, task_id)
    with locked_state(write=True) as data:
        policy = data["boards"].get(board)
        if policy is None:
            raise ValueError("review escalation requires an active board policy")
        settings = _escalation_settings(policy.get("escalation", {"enabled": False, "normal_correction_limit": MAX_CHANGES,
                                                                     "max_attempts": 1, "implementation_profile": None,
                                                                     "reviewer_profile": None}))
        entry = data["escalations"].get(key)
        if (not isinstance(entry, dict) or entry.get("binding") != binding
                or current_escalation_attempt(entry).get("intent", {}).get("review_run_id") != review_run_id):
            raise ValueError("review escalation intent is absent or mismatched")
        attempt = current_escalation_attempt(entry)
        if attempt.get("intent", {}).get("status") == "routed":
            return "routed"
        if attempt.get("intent", {}).get("status") != "changes_requested_pending":
            raise ValueError("review escalation intent has invalid status")
        if not settings["enabled"]:
            return "held"
        if not reconcile(json.loads(json.dumps(entry))):
            return "pending"
        route = {"board": board, "task_id": task_id, "attempt": attempt["attempt"],
                 "implementation_profile": attempt["implementation_profile"], "reviewer_profile": attempt["reviewer_profile"]}
        data["effective_routing"][key] = route
        attempt["intent"] = dict(attempt["intent"], status="routed")
        return "routed"


def finalize_escalation_routing(board: str, task_id: str, *, review_run_id: int) -> dict[str, Any]:
    """Compatibility finalizer for callers with already-proven native routing."""
    key = binding_key(board, task_id)
    with locked_state(write=True) as data:
        entry = data["escalations"].get(key)
        if not entry or current_escalation_attempt(entry).get("intent", {}).get("review_run_id") != review_run_id:
            raise ValueError("review escalation intent is absent or mismatched")
        attempt = current_escalation_attempt(entry)
        route = {"board": board, "task_id": task_id, "attempt": attempt["attempt"],
                 "implementation_profile": attempt["implementation_profile"], "reviewer_profile": attempt["reviewer_profile"]}
        data["effective_routing"][key] = route
        attempt["intent"] = dict(attempt["intent"], status="routed")
        return json.loads(json.dumps(route))

def recovery_entry(task_id: str, board: str | None = None, *, failed_run_id: int | None = None, phase: str | None = None) -> dict[str, Any] | None:
    data = load_state(); board = board or board_name()
    if failed_run_id is not None and phase is not None: return data["recovery"].get(recovery_key(board, task_id, failed_run_id, phase))
    matches = [v for v in data["recovery"].values() if v.get("board") == board and v.get("task_id") == task_id and not v.get("terminal")]
    return json.loads(json.dumps(matches[-1])) if len(matches) == 1 else None

def reserve_recovery(board: str, task_id: str, *, failed_run_id: int, phase: str, workspace_path: str, checkpoint: dict[str, Any], binding: dict[str, Any], adopted: bool) -> dict[str, Any]:
    """Atomically preserve binding, checkpoint, intent, budget, and workspace lease."""
    key = recovery_key(board, task_id, failed_run_id, phase); budget = phase_budget_key(board, task_id, phase)
    with locked_state(write=True) as data:
        existing = data["recovery"].get(key)
        if existing is not None: return json.loads(json.dumps(existing))
        policy = data["boards"].get(board)
        if policy is None:
            raise ValueError("recovery requires an active board policy")
        maximum = _recovery_settings(policy.get("recovery", {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}))["max_per_phase"]
        if data["recovery_budgets"].get(budget, 0) >= maximum:
            raise ValueError("recovery budget exhausted for this task phase; operator action is required")
        holder = data["workspace_leases"].get(workspace_path)
        if holder is not None and holder != key: raise ValueError("workspace recovery lease is held by another task")
        task_key = binding_key(board, task_id)
        current = data["tasks"].get(task_key)
        binding_workspace = binding.get("workspace_path") if isinstance(binding, dict) else None
        try:
            same_workspace = (isinstance(binding_workspace, str) and Path(binding_workspace).is_absolute()
                              and Path(binding_workspace).resolve() == Path(workspace_path).resolve())
        except (OSError, RuntimeError):
            same_workspace = False
        if not same_workspace:
            raise ValueError("recovery workspace differs from its immutable task binding")
        if _workspace_binding_conflict(data, board, task_id, workspace_path):
            raise ValueError("task-scoped worktree is already bound to another managed task")
        if current is None:
            if not adopted: raise ValueError("recovery requires an immutable binding")
            if not Path(workspace_path).is_absolute() or not _is_materialized_linked_worktree(workspace_path):
                raise ValueError("adopted recovery requires a materialized task-scoped linked worktree")
            data["tasks"][task_key] = dict(binding)
        elif current != binding: raise ValueError("existing binding differs from recovery observation")
        record = {"board": board, "task_id": task_id, "failed_run_id": failed_run_id, "phase": phase,
                  "workspace_path": workspace_path, "checkpoint": checkpoint, "adopted": adopted,
                  "intent": {"status": "unblock_requested"}, "expected_source_status": "review" if phase == "review" else "ready"}
        data["recovery"][key] = record; data["recovery_budgets"][budget] = data["recovery_budgets"].get(budget, 0) + 1
        data["workspace_leases"][workspace_path] = key
        return json.loads(json.dumps(record))

def update_recovery_identity(board: str, task_id: str, failed_run_id: int, phase: str, **fields: Any) -> dict[str, Any]:
    with locked_state(write=True) as data:
        record = data["recovery"].get(recovery_key(board, task_id, failed_run_id, phase))
        if record is None: raise ValueError("recovery intent is absent")
        record.update(fields); return json.loads(json.dumps(record))

def authorize_recovery_run(board: str, task_id: str, failed_run_id: int, phase: str, run_id: int, profile: str) -> None:
    with locked_state(write=True) as data:
        record = data["recovery"].get(recovery_key(board, task_id, failed_run_id, phase))
        if record is not None and record.get("authorized_run_id") is None:
            record["authorized_run_id"] = run_id; record["authorized_profile"] = profile

def pin_recovery_receipt(board: str, task_id: str, failed_run_id: int, phase: str, run_id: int, receipt: dict[str, Any]) -> None:
    with locked_state(write=True) as data:
        record = data["recovery"].get(recovery_key(board, task_id, failed_run_id, phase))
        if record is None or record.get("receipt") is not None: return
        if record.get("authorized_run_id") != run_id: raise ValueError("recovery receipt belongs to a different run")
        record["receipt"] = dict(receipt, run_id=run_id)

def terminalize_recovery(board: str, task_id: str, failed_run_id: int, phase: str, reason: str) -> None:
    with locked_state(write=True) as data:
        key = recovery_key(board, task_id, failed_run_id, phase); record = data["recovery"].get(key)
        if record is not None:
            record["terminal"] = reason
            if data["workspace_leases"].get(record["workspace_path"]) == key: del data["workspace_leases"][record["workspace_path"]]


def terminalize_implementation_handoff_recovery(board: str, task_id: str, implementation_run_id: int) -> bool:
    """Consume only the recovered implementation lease proven by its own receipt.

    The native handoff has already ended ``implementation_run_id`` as
    ``review_requested``.  This function does not inspect or infer native state;
    its caller must do that readback first.  It atomically rejects a wrong phase,
    run, missing receipt, or ambiguous active recovery instead of releasing a
    different lease.  The phase budget deliberately remains consumed.
    """
    if type(implementation_run_id) is not int or implementation_run_id <= 0:
        raise ValueError("implementation handoff recovery run is invalid")
    with locked_state(write=True) as data:
        matches = [record for record in data["recovery"].values()
                   if record.get("board") == board and record.get("task_id") == task_id and not record.get("terminal")]
        if not matches:
            return False
        if len(matches) != 1:
            raise ValueError("implementation handoff recovery is ambiguous")
        record = matches[0]
        receipt = record.get("receipt")
        if (record.get("phase") != "implementation" or record.get("authorized_run_id") != implementation_run_id
                or not isinstance(receipt, dict) or receipt.get("run_id") != implementation_run_id):
            raise ValueError("implementation handoff recovery does not match the exact authorized receipt")
        key = recovery_key(board, task_id, record["failed_run_id"], "implementation")
        record["terminal"] = "native_review_handoff"
        if data["workspace_leases"].get(record["workspace_path"]) == key:
            del data["workspace_leases"][record["workspace_path"]]
        return True


def is_managed(task_id: str | None, board: str | None = None) -> bool:
    if not task_id: return False
    data = load_state(); selected = board or board_name()
    return binding_key(selected, task_id) in data["tasks"] or selected in data["boards"]

def activate_board(board: str, *, activation_id: str, native_run_watermark: int | None = None) -> dict[str, Any]:
    if not _text(board) or not _text(activation_id) or (native_run_watermark is not None and (type(native_run_watermark) is not int or native_run_watermark < 0)): raise ValueError("board activation has invalid identity or run watermark")
    with locked_state(write=True) as data:
        if native_run_watermark is None:
            from .native import board_run_watermark
            native_run_watermark = board_run_watermark(board)
        implementation, reviewer = data.get("implementation_profile"), data.get("reviewer_profile")
        if not _text(implementation) or not _text(reviewer): raise ValueError("save implementation and reviewer profiles before board activation")
        if not profile_exists(implementation) or not profile_exists(reviewer): raise ValueError("configured worker profile is missing; save valid Hermes profiles before board activation")
        policy = {"activation_id": activation_id, "native_run_watermark": native_run_watermark, "implementation_profile": implementation, "reviewer_profile": reviewer, "recovery": {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}, "escalation": {"enabled": False, "normal_correction_limit": MAX_CHANGES, "max_attempts": 1, "implementation_profile": None, "reviewer_profile": None}, "runtime_escalation": {"enabled": False, "max_attempts": 1, "implementation_profile": None, "reviewer_profile": None}}
        data["boards"][board] = policy; return json.loads(json.dumps(policy))

def _workspace_binding_conflict(data: dict[str, Any], board: str, task_id: str, workspace: str) -> bool:
    target = Path(workspace).resolve()
    for binding in data["tasks"].values():
        if binding.get("board") == board and binding.get("task_id") == task_id:
            continue
        other = Path(str(binding.get("workspace_path"))).resolve()
        if target == other:
            return True
    return False


def _is_materialized_linked_worktree(workspace: str) -> bool:
    try:
        path = Path(workspace).resolve()
    except (OSError, RuntimeError):
        return False
    try:
        top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        git_dir = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir"],
                                 check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        common_dir = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(top and git_dir and common_dir and Path(top).resolve() == path
                and Path(git_dir).resolve() != Path(common_dir).resolve())


def _validate_first_run(policy: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]], *, run_id: int, profile: str) -> tuple[str, str]:
    if not isinstance(task, dict) or not isinstance(runs, list) or type(run_id) is not int or not _text(profile): raise ValueError("native task/run observation is incomplete")
    task_id, workspace = task.get("id"), task.get("workspace_path")
    if not _text(task_id) or task.get("status") != "running": raise ValueError("board policy cannot adopt a non-running task")
    if task.get("workspace_kind") != "worktree" or not _text(workspace) or not Path(workspace).is_absolute(): raise ValueError("board policy requires an absolute task-scoped worktree: Git workspace")
    if not _is_materialized_linked_worktree(str(workspace)): raise ValueError("board policy requires a materialized task-scoped linked worktree")
    if task.get("assignee") != policy["implementation_profile"] or profile != policy["implementation_profile"]: raise ValueError("board policy task/profile does not match its pinned implementation profile")
    if len(runs) != 1: raise ValueError("board policy will not adopt a task with prior or ambiguous native runs")
    run = runs[0]
    if (not isinstance(run, dict) or run.get("id") != run_id or run.get("profile") != profile or run.get("status") != "running" or run.get("ended_at") is not None or run_id <= policy["native_run_watermark"]): raise ValueError("board policy requires one exact implementation run created after its activation watermark")
    return task_id, workspace

def bind_first_owned_run(board: str, task: dict[str, Any], runs: list[dict[str, Any]], *, run_id: int, profile: str) -> dict[str, Any]:
    with locked_state(write=True) as data:
        policy = data["boards"].get(board)
        if policy is None: raise ValueError("board has no active review-gate policy")
        task_id = task.get("id") if isinstance(task, dict) else None
        if not _text(task_id): raise ValueError("native task id is required")
        key = binding_key(board, task_id); existing = data["tasks"].get(key)
        if existing is not None: return json.loads(json.dumps(existing))
        task_id, workspace = _validate_first_run(policy, task, runs, run_id=run_id, profile=profile)
        if _workspace_binding_conflict(data, board, str(task_id), str(workspace)):
            raise ValueError("task-scoped worktree is already bound to another managed task")
        binding = {"board": board, "task_id": task_id, "implementation_profile": policy["implementation_profile"], "reviewer_profile": policy["reviewer_profile"], "workspace_path": workspace, "policy_activation_id": policy["activation_id"], "policy_native_run_watermark": policy["native_run_watermark"], "native_run_id": run_id}
        data["tasks"][key] = binding; return json.loads(json.dumps(binding))

def enroll_task(*, board: str, task: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(task, dict) or not isinstance(runs, list): raise ValueError("native task and runs are required for enrollment")
    task_id, workspace = task.get("id"), task.get("workspace_path")
    if not _text(task_id) or not _text(workspace): raise ValueError("native task id and workspace_path are required")
    if task.get("status") != "blocked" or task.get("block_kind") != "needs_input": raise ValueError("enrollment requires an operator-parked blocked needs_input task")
    if runs: raise ValueError("enrollment is unsafe after any native run; create a new blocked card")
    if task.get("workspace_kind") != "worktree" or not Path(str(workspace)).is_absolute(): raise ValueError("managed work requires an absolute task-scoped worktree: Git workspace")
    if not _is_materialized_linked_worktree(str(workspace)):
        raise ValueError("enrollment requires a materialized linked worktree; use worktree:<repo-root> and native dispatch to resolve one first")
    with locked_state(write=True) as data:
        implementation, reviewer = data.get("implementation_profile"), data.get("reviewer_profile")
        if not _text(implementation) or not _text(reviewer) or not profile_exists(implementation) or not profile_exists(reviewer): raise ValueError("configured worker profiles are invalid; save valid Hermes profiles before enrollment")
        if task.get("assignee") != implementation: raise ValueError("parked task must be assigned to the configured implementation profile")
        key = binding_key(board, task_id)
        if key in data["tasks"]: raise ValueError("task is already enrolled")
        if _workspace_binding_conflict(data, board, str(task_id), str(workspace)):
            raise ValueError("task-scoped worktree is already bound to another managed task")
        binding = {"board": board, "task_id": task_id, "implementation_profile": implementation, "reviewer_profile": reviewer, "workspace_path": workspace}
        data["tasks"][key] = binding; return json.loads(json.dumps(binding))

def profile_exists(name: str) -> bool:
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    try: return name in list_profile_names() and (get_profile_dir(name) / "config.yaml").is_file()
    except (TypeError, ValueError): return False

def profiles() -> list[str]:
    from hermes_cli.profiles import list_profile_names
    return list_profile_names()

def worker_profile() -> str | None:
    value = os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_PROFILE_NAME")
    if value: return value
    from hermes_cli.profiles import current_profile_name
    return current_profile_name()
