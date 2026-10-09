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
from typing import Any, Iterator

CONFIG_NAME = "local-first-review.json"
MAX_CHANGES = 2
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
    return {"version": 4, "implementation_profile": None, "reviewer_profile": None,
            "tasks": {}, "boards": {}, "recovery": {}, "recovery_budgets": {}, "workspace_leases": {}}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _recovery_settings(value: Any) -> dict[str, Any]:
    """Validate the persisted board policy; zero is never a pause surrogate."""
    if not isinstance(value, dict) or set(value) != {"enabled", "max_per_phase"}:
        raise ValueError("review recovery policy is invalid")
    maximum = value.get("max_per_phase")
    if (type(value.get("enabled")) is not bool or type(maximum) is not int
            or not 1 <= maximum <= RECOVERY_MAX_PER_PHASE_LIMIT):
        raise ValueError("review recovery policy is invalid")
    return {"enabled": value["enabled"], "max_per_phase": maximum}


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
    return data


def _validate(data: Any) -> dict[str, Any]:
    data = _migrate(data)
    required_roots = ("boards", "recovery", "recovery_budgets", "workspace_leases")
    if data.get("version") != 4 or any(not isinstance(data.get(k), dict) for k in required_roots):
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
                or policy["native_run_watermark"] < 0 or policy["implementation_profile"] == policy["reviewer_profile"]):
            raise ValueError("review-gate board policy is invalid")
        _recovery_settings(policy.get("recovery", {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}))
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
        if current is None:
            if not adopted: raise ValueError("recovery requires an immutable binding")
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
        if not _text(implementation) or not _text(reviewer) or implementation == reviewer: raise ValueError("save distinct implementation and reviewer profiles before board activation")
        if not profile_exists(implementation) or not profile_exists(reviewer): raise ValueError("configured worker profile is missing; save valid Hermes profiles before board activation")
        policy = {"activation_id": activation_id, "native_run_watermark": native_run_watermark, "implementation_profile": implementation, "reviewer_profile": reviewer, "recovery": {"enabled": False, "max_per_phase": RECOVERY_MAX_PER_PHASE}}
        data["boards"][board] = policy; return json.loads(json.dumps(policy))

def _validate_first_run(policy: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]], *, run_id: int, profile: str) -> tuple[str, str]:
    if not isinstance(task, dict) or not isinstance(runs, list) or type(run_id) is not int or not _text(profile): raise ValueError("native task/run observation is incomplete")
    task_id, workspace = task.get("id"), task.get("workspace_path")
    if not _text(task_id) or task.get("status") != "running": raise ValueError("board policy cannot adopt a non-running task")
    if task.get("workspace_kind") != "dir" or not _text(workspace) or not Path(workspace).is_absolute(): raise ValueError("board policy requires an absolute dir: Git workspace")
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
        binding = {"board": board, "task_id": task_id, "implementation_profile": policy["implementation_profile"], "reviewer_profile": policy["reviewer_profile"], "workspace_path": workspace, "policy_activation_id": policy["activation_id"], "policy_native_run_watermark": policy["native_run_watermark"], "native_run_id": run_id}
        data["tasks"][key] = binding; return json.loads(json.dumps(binding))

def enroll_task(*, board: str, task: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(task, dict) or not isinstance(runs, list): raise ValueError("native task and runs are required for enrollment")
    task_id, workspace = task.get("id"), task.get("workspace_path")
    if not _text(task_id) or not _text(workspace): raise ValueError("native task id and workspace_path are required")
    if task.get("status") != "blocked" or task.get("block_kind") != "needs_input": raise ValueError("enrollment requires an operator-parked blocked needs_input task")
    if runs: raise ValueError("enrollment is unsafe after any native run; create a new blocked card")
    if task.get("workspace_kind") != "dir" or not Path(workspace).is_absolute(): raise ValueError("managed work requires an absolute dir: Git workspace")
    with locked_state(write=True) as data:
        implementation, reviewer = data.get("implementation_profile"), data.get("reviewer_profile")
        if not _text(implementation) or not _text(reviewer) or implementation == reviewer or not profile_exists(implementation) or not profile_exists(reviewer): raise ValueError("configured worker profiles are invalid; save valid distinct Hermes profiles before enrollment")
        if task.get("assignee") != implementation: raise ValueError("parked task must be assigned to the configured implementation profile")
        key = binding_key(board, task_id)
        if key in data["tasks"]: raise ValueError("task is already enrolled")
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
