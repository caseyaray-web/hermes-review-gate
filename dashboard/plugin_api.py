"""Read-only dashboard telemetry plus explicit configuration and enrollment."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import sqlite3
import time
import uuid
from pathlib import Path
import sys
from typing import Any

# Hermes mounts dashboard APIs before loading a directory plugin's __init__.
# Resolve the shipped runtime beside this dashboard, not from the checkout cwd.
_plugin_root = str(Path(__file__).resolve().parent.parent)
if _plugin_root not in sys.path:
    sys.path.insert(0, _plugin_root)

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from local_first_review.state import (ESCALATION_MAX_ATTEMPTS_LIMIT, MAX_CHANGES, RECOVERY_MAX_PER_PHASE_LIMIT,
                                      activate_board, current_escalation_attempt, effective_routing, enroll_task, escalation_entry, load_state, locked_state, profile_exists, profiles,
                                      set_escalation_policy, set_recovery_policy, trusted_routing)

router = APIRouter()
COUNT_NAMES = (
    "implementation_active", "awaiting_or_under_review", "changes_requested",
    "approved_done", "blocked_failed",
)
PROCESS_LIVENESS = "unknown (not probed)"
# Match native Kanban's wedged-worker heartbeat boundary.  Telemetry never
# probes a PID, so an old or absent activity record is explicitly uncertain.
ACTIVE_RUN_STALE_SECONDS = 60 * 60


class Configuration(BaseModel):
    implementation_profile: str = Field(min_length=1, max_length=128)
    reviewer_profile: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class Enrollment(BaseModel):
    board: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class BoardActivation(BaseModel):
    board: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")


class RecoveryControl(BaseModel):
    board: str = Field(min_length=1, max_length=64)
    enabled: StrictBool
    max_per_phase: StrictInt | None = Field(default=None, ge=1, le=RECOVERY_MAX_PER_PHASE_LIMIT)

    model_config = ConfigDict(extra="forbid")


class EscalationControl(BaseModel):
    board: str = Field(min_length=1, max_length=64)
    enabled: StrictBool
    normal_correction_limit: StrictInt = Field(ge=1, le=MAX_CHANGES)
    max_attempts: StrictInt = Field(ge=1, le=ESCALATION_MAX_ATTEMPTS_LIMIT)
    implementation_profile: str | None = Field(default=None, min_length=1, max_length=128)
    reviewer_profile: str | None = Field(default=None, min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class TelemetryUnavailable(RuntimeError):
    """A safe, user-facing failure to observe native Kanban."""


def _plain(record: Any) -> dict[str, Any]:
    return asdict(record) if is_dataclass(record) else dict(record)


def _is_archived(task: dict[str, Any]) -> bool:
    """Keep native archive state out of dashboard presentation, whatever its shape."""
    return task.get("status") == "archived" or task.get("archived_at") is not None


def _observe_task(board: str, task_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Read one native card through public APIs over a non-initializing DB handle."""
    try:
        from hermes_cli import kanban_db
        path = kanban_db.kanban_db_path(board=board)
        # Do not use kanban_db.connect(): it may initialize/migrate a board.
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            task = kanban_db.get_task(conn, task_id)
            if task is None:
                return None, [], []
            return _plain(task), [_plain(run) for run in kanban_db.list_runs(conn, task_id)], [
                _plain(event) for event in kanban_db.list_events(conn, task_id)
            ]
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, ImportError) as exc:
        # Do not expose paths, environment, or raw database errors in the dashboard.
        raise TelemetryUnavailable("Kanban telemetry is unavailable; verify the selected board is initialized") from exc


def _observe_board(board: str) -> list[dict[str, Any]]:
    """Read board cards without creating, migrating, or mutating a native board."""
    try:
        from hermes_cli import kanban_db
        path = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            return [_plain(task) for task in kanban_db.list_tasks(conn, include_archived=True)]
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, ImportError) as exc:
        raise TelemetryUnavailable("Kanban board telemetry is unavailable; verify the selected board is initialized") from exc


def _board_runs(board: str, task_id: str) -> list[dict[str, Any]]:
    """Read a card's run history independently so preview classifications stay native-derived."""
    try:
        from hermes_cli import kanban_db
        path = kanban_db.kanban_db_path(board=board)
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            return [_plain(run) for run in kanban_db.list_runs(conn, task_id)]
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, ImportError) as exc:
        raise TelemetryUnavailable("Kanban board telemetry is unavailable; verify the selected board is initialized") from exc


def _attention(task_id: str, reason: str, next_step: str) -> dict[str, str]:
    return {"task_id": task_id, "reason": reason, "next_step": next_step}


def _activation_preview(board: str, *, policy: dict[str, Any], legacy_bound_ids: set[str] | None = None) -> dict[str, Any]:
    """Classify cards by the same first-run evidence required for policy binding."""
    eligible_no_run: list[str] = []
    awaiting_first_gate: list[str] = []
    attention: list[dict[str, str]] = []
    history: list[str] = []
    legacy_bound: list[str] = []
    legacy_bound_ids = legacy_bound_ids or set()
    implementation_profile = policy["implementation_profile"]
    watermark = policy["native_run_watermark"]
    for task in _observe_board(board):
        if _is_archived(task):
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        status = task.get("status")
        runs = _board_runs(board, task_id)
        if status == "done":
            history.append(task_id)
            continue
        # Already bound work—including a review or exhausted-review hold—has
        # immutable/effective routing. It must not receive generic first-run
        # "assign implementation profile" guidance from activation telemetry.
        if task_id in legacy_bound_ids:
            legacy_bound.append(task_id)
            continue
        workspace = task.get("workspace_path")
        if task.get("workspace_kind") != "dir" or not isinstance(workspace, str) or not Path(workspace).is_absolute():
            attention.append(_attention(task_id, "workspace is not an absolute dir: workspace",
                                        "Set an absolute dir: workspace before native dispatch."))
            continue
        assignee = task.get("assignee")
        if not isinstance(assignee, str) or not assignee:
            attention.append(_attention(task_id, "task is unassigned; assign the pinned implementation profile",
                                        f"Assign {implementation_profile} before native dispatch."))
            continue
        if assignee != implementation_profile:
            attention.append(_attention(task_id, "task is assigned to another profile",
                                        f"Assign the pinned implementation profile {implementation_profile}."))
            continue
        if not runs:
            if status in {"todo", "ready"}:
                eligible_no_run.append(task_id)
            else:
                attention.append(_attention(task_id, f"native status {status or 'unknown'} is not eligible for dispatch",
                                            "Use native Kanban to resolve the card status before dispatch."))
            continue
        if status != "running":
            attention.append(_attention(task_id, "task already has native run history",
                                        "Do not adopt executed work; inspect native history or create a new card."))
            continue
        if len(runs) != 1:
            attention.append(_attention(task_id, "task has prior or ambiguous native run history",
                                        "Do not adopt executed work; inspect native history or create a new card."))
            continue
        run = runs[0]
        run_id = run.get("id") if isinstance(run, dict) else None
        current_run_id = task.get("current_run_id")
        if type(run_id) is not int or run_id <= watermark:
            attention.append(_attention(task_id, "current run predates this policy activation",
                                        "Do not adopt pre-activation work; create a new post-activation card."))
        elif current_run_id != run_id:
            attention.append(_attention(task_id, "native current run does not match the observed first run",
                                        "Refresh native history; do not adopt an ambiguous run."))
        elif run.get("profile") != implementation_profile:
            attention.append(_attention(task_id, "current run is owned by another profile",
                                        f"Only {implementation_profile}'s exact first run can be gated."))
        elif run.get("status") != "running" or run.get("ended_at") is not None:
            attention.append(_attention(task_id, "observed first run is no longer active",
                                        "Do not adopt an ended run; inspect native history."))
        else:
            awaiting_first_gate.append(task_id)
    return {"board": board, "eligible_no_run": sorted(eligible_no_run),
            "awaiting_first_gate": sorted(awaiting_first_gate),
            "attention": sorted(attention, key=lambda item: item["task_id"]), "history": sorted(history),
            "legacy_bound": sorted(legacy_bound),
            "message": "Board policy telemetry is read-only. Only an exact pinned, active first run after the watermark can bind at its first gated lifecycle call."}


def _policy_scope_view(board: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Read the persistent policy scope and its current native classification."""
    try:
        state = load_state()
        legacy_bound_ids = {binding["task_id"] for binding in state["tasks"].values()
                            if binding.get("board") == board}
        classification = _activation_preview(board, policy=policy, legacy_bound_ids=legacy_bound_ids)
        return {"board": board, "activation_id": policy["activation_id"],
                "native_run_watermark": policy["native_run_watermark"],
                "implementation_profile": policy["implementation_profile"],
                "reviewer_profile": policy["reviewer_profile"], "recovery": policy.get("recovery", {"enabled": False, "max_per_phase": 1}),
                "escalation": policy.get("escalation", {"enabled": False, "normal_correction_limit": MAX_CHANGES, "max_attempts": 1, "implementation_profile": None, "reviewer_profile": None}),
                "eligible_no_run": classification["eligible_no_run"],
                "awaiting_first_gate": classification["awaiting_first_gate"],
                "attention": classification["attention"], "history": classification["history"],
                "legacy_bound": classification["legacy_bound"], "error": None}
    except TelemetryUnavailable as exc:
        return {"board": board, "activation_id": policy["activation_id"],
                "native_run_watermark": policy["native_run_watermark"],
                "implementation_profile": policy["implementation_profile"],
                "reviewer_profile": policy["reviewer_profile"],
                "recovery": policy.get("recovery", {"enabled": False, "max_per_phase": 1}),
                "escalation": policy.get("escalation", {"enabled": False, "normal_correction_limit": MAX_CHANGES, "max_attempts": 1, "implementation_profile": None, "reviewer_profile": None}),
                "eligible_no_run": None, "awaiting_first_gate": None, "attention": None,
                "history": None, "legacy_bound": None, "error": str(exc)}


def _lfr_metadata(runs: list[dict[str, Any]]) -> dict[str, Any]:
    for run in reversed(runs):
        metadata = run.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("local_first_review"), dict):
            return metadata["local_first_review"]
    return {}


def _claimed_source_status(events: list[dict[str, Any]], run_id: Any) -> str | None:
    for event in reversed(events):
        if event.get("kind") != "claimed" or event.get("run_id") != run_id:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("source_status"), str):
            return payload["source_status"]
    return None


def _completion_is_approved(task: dict[str, Any], runs: list[dict[str, Any]], binding: dict[str, Any]) -> bool:
    """Only native completion carrying the gate's approval can make this done."""
    if task.get("status") != "done":
        return False
    try:
        route = trusted_routing(binding["task_id"], binding["board"], binding)
    except (KeyError, ValueError):
        return False
    for run in reversed(runs):
        if run.get("outcome") != "completed" or run.get("ended_at") is None:
            continue
        metadata = run.get("metadata")
        review = metadata.get("local_first_review") if isinstance(metadata, dict) else None
        implementation_run_id = review.get("implementation_run_id") if isinstance(review, dict) else None
        handoff = next((candidate for candidate in runs if candidate.get("id") == implementation_run_id), None)
        handoff_metadata = handoff.get("metadata") if isinstance(handoff, dict) else None
        handoff_review = handoff_metadata.get("local_first_review") if isinstance(handoff_metadata, dict) else None
        return bool(
            isinstance(review, dict) and review.get("verdict") == "approved"
            and review.get("reviewer_run_id") == run.get("id")
            and review.get("reviewer_profile") == run.get("profile")
            and isinstance(review.get("reviewer_profile"), str)
            and isinstance(implementation_run_id, int) and isinstance(handoff, dict)
            and isinstance(handoff_review, dict)
            and handoff.get("profile") == handoff_review.get("implementation_profile")
            and handoff.get("outcome") == "review_requested"
            and isinstance(handoff_review, dict)
            and handoff_review.get("implementation_run_id") == implementation_run_id
            and handoff_review.get("implementation_profile") == route["implementation_profile"]
            and handoff_review.get("reviewer_profile") == route["reviewer_profile"]
            and review.get("candidate") == handoff_review.get("candidate")
        )
    return False


def _phase(task: dict[str, Any], runs: list[dict[str, Any]], events: list[dict[str, Any]], approved: bool) -> str:
    status = task.get("status")
    active = next((run for run in reversed(runs) if run.get("ended_at") is None), None)
    if active is not None:
        return "review" if _claimed_source_status(events, active.get("id")) == "review" else "implementation"
    latest = runs[-1] if runs else None
    if status == "done":
        return "approved_done" if approved else "unknown"
    if status in {"blocked", "triage", "failed"} or (latest and latest.get("outcome") in {"failed", "error", "crashed", "timed_out"}):
        return "blocked_failed"
    if latest and latest.get("outcome") == "changes_requested":
        return "changes_requested"
    if status == "review" or (latest and latest.get("outcome") == "review_requested"):
        return "awaiting_review"
    return "unknown"


def _last_native_activity(task: dict[str, Any], runs: list[dict[str, Any]], events: list[dict[str, Any]]) -> int | None:
    values = [task.get(name) for name in ("created_at", "started_at", "completed_at", "last_heartbeat_at")]
    for run in runs:
        values.extend((run.get("started_at"), run.get("ended_at"), run.get("last_heartbeat_at")))
    values.extend(event.get("created_at") for event in events)
    numeric = [value for value in values if isinstance(value, int)]
    return max(numeric) if numeric else None


def _active_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((run for run in reversed(runs) if run.get("ended_at") is None), None)


def _active_run_is_stale(active: dict[str, Any] | None, observed_at: int) -> bool:
    if active is None:
        return False
    activity = active.get("last_heartbeat_at") or active.get("started_at")
    return not isinstance(activity, int) or observed_at - activity > ACTIVE_RUN_STALE_SECONDS


def _next_step(phase: str, error: str | None) -> str:
    if error and "profile" in error.lower():
        return "Restore the bound Hermes profile, then refresh before dispatching another worker."
    if error and "stale" in error.lower():
        return "Verify the active native worker or reclaim the stale Kanban run before continuing."
    if error and phase == "unknown":
        return "Inspect the native board/task and restore missing profiles or board access, then refresh."
    return {
        "implementation": "Wait for the implementation worker to finish through the review handoff.",
        "awaiting_review": "Wait for the configured reviewer to claim the native review.",
        "review": "Wait for the reviewer verdict.",
        "changes_requested": "Address the reviewer feedback and submit a fresh implementation handoff.",
        "blocked_failed": "Inspect the native task failure or hold and resolve it through Kanban.",
        "approved_done": "No review-gate action is required.",
        "unknown": "Inspect the native task history; this snapshot cannot infer a managed phase.",
    }[phase]


def _task_view(binding: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    board, task_id = binding["board"], binding["task_id"]
    published_route = effective_routing(task_id, board)
    try:
        route = trusted_routing(task_id, board, binding)
    except ValueError:
        route = binding
    if published_route is not None and any(published_route.get(key) != route.get(key)
                                          for key in ("board", "task_id", "implementation_profile", "reviewer_profile")):
        published_route = None
    pending = escalation_entry(task_id, board)
    escalation_status = current_escalation_attempt(pending).get("intent", {}).get("status") if isinstance(pending, dict) else None
    policy = load_state().get("boards", {}).get(board, {})
    escalation_enabled = policy.get("escalation", {}).get("enabled") is True
    base = {"board": board, "task_id": task_id, "implementation_profile": route["implementation_profile"],
            "reviewer_profile": route["reviewer_profile"],
            "original_implementation_profile": binding["implementation_profile"],
            "original_reviewer_profile": binding["reviewer_profile"], "effective_routing": published_route,
            "escalation_status": escalation_status, "escalation_enabled": escalation_enabled, "process_liveness": PROCESS_LIVENESS}
    try:
        task, runs, events = _observe_task(board, task_id)
    except TelemetryUnavailable as exc:
        base.update({"title": None, "native_status": "unavailable", "phase": "unknown", "run_status": "unknown",
                     "summary": None, "verdict": None, "reason": None, "changes": None, "last_observed": None,
                     "error": str(exc), "next_step": _next_step("unknown", str(exc))})
        return base, False
    if task is None:
        base.update({"title": None, "native_status": "unavailable", "phase": "unknown", "run_status": "unknown",
                     "summary": None, "verdict": None, "reason": None, "changes": None, "last_observed": None,
                     "error": "Native task is absent from the selected board", "next_step": _next_step("unknown", "absent")})
        return base, False
    if _is_archived(task):
        return None, True
    observed_at = int(time.time())
    latest = runs[-1] if runs else None
    active = _active_run(runs)
    metadata = _lfr_metadata(runs)
    verdict = metadata.get("verdict") if isinstance(metadata.get("verdict"), str) else None
    reason = metadata.get("rationale") if isinstance(metadata.get("rationale"), str) else None
    for prior in reversed(runs):
        if prior.get("outcome") == "completed":
            break
        if prior.get("outcome") == "changes_requested" or str(prior.get("summary") or "").startswith("Review changes exhausted;"):
            verdict, reason = "changes_requested", prior.get("summary")
            break
    summary = next((run.get("summary") for run in reversed(runs) if run.get("summary")), task.get("result"))
    handoff_summary = next((run.get("summary") for run in reversed(runs) if run.get("outcome") == "review_requested"), None)
    changes = sum(run.get("outcome") == "changes_requested" for run in runs)
    approved = _completion_is_approved(task, runs, binding)
    phase = _phase(task, runs, events, approved)
    stale = _active_run_is_stale(active, observed_at)
    try:
        missing_profiles = [route[key] for key in ("implementation_profile", "reviewer_profile")
                            if not profile_exists(route[key])]
    except (OSError, ValueError, RuntimeError, ImportError):
        missing_profiles = None
    has_current_failure = phase == "blocked_failed" or bool(active and active.get("error"))
    error = ("Bound Hermes profile validation is unavailable; verify the local profile configuration."
             if missing_profiles is None else
             "A bound Hermes profile is missing or deleted; restore it before continuing this task."
             if missing_profiles else
             "Native active run is stale or has no observable activity; verify or reclaim the worker."
             if stale else "Native worker reported a current failure or hold; inspect the native task." if has_current_failure else None)
    base.update({"title": task.get("title"), "native_status": task.get("status"), "phase": phase,
                 "run_status": "running" if active else "ended" if latest else "not_started",
                 "run_outcome": latest.get("outcome") if latest else None, "summary": summary, "verdict": verdict,
                 "handoff_summary": handoff_summary,
                 "reason": reason, "changes": changes, "last_observed": observed_at,
                 "last_native_activity": _last_native_activity(task, runs, events),
                 "error": error, "next_step": _next_step(phase, error),
                 # Explicit aliases make the API self-describing without breaking shipped UI fields.
                 "latest_summary": summary, "latest_verdict": verdict, "latest_reason": reason,
                 "changes_count": changes})
    return base, not stale and missing_profiles == []


def _view() -> dict[str, Any]:
    state = load_state()
    tasks, fully_observed = [], True
    for binding in state["tasks"].values():
        item, observed = _task_view(binding)
        if item is not None:
            tasks.append(item)
        fully_observed = fully_observed and observed
    counts: dict[str, int | None] = {name: 0 for name in COUNT_NAMES} if fully_observed else {name: None for name in COUNT_NAMES}
    if fully_observed:
        for item in tasks:
            phase = item["phase"]
            if phase == "implementation": counts["implementation_active"] += 1
            elif phase in {"awaiting_review", "review"}: counts["awaiting_or_under_review"] += 1
            elif phase == "changes_requested": counts["changes_requested"] += 1
            elif phase == "approved_done": counts["approved_done"] += 1
            elif phase == "blocked_failed": counts["blocked_failed"] += 1
    board_policies = [_policy_scope_view(board, policy) for board, policy in sorted(state.get("boards", {}).items())]
    return {"configuration": {"implementation_profile": state.get("implementation_profile"),
                                "reviewer_profile": state.get("reviewer_profile"), "change_limit": MAX_CHANGES},
            "board_policies": board_policies, "tasks": tasks, "counts": counts,
            "telemetry": "read-only native Kanban snapshots; process liveness is not probed"}


@router.get("/profiles")
def get_profiles() -> dict[str, Any]:
    try:
        return {"profiles": profiles()}
    except (OSError, ValueError, RuntimeError, ImportError):
        raise HTTPException(503, "Hermes profiles unavailable; verify the local Hermes profile configuration") from None


@router.get("/status")
def status() -> dict[str, Any]:
    try:
        return _view()
    except (OSError, ValueError, RuntimeError, ImportError):
        raise HTTPException(503, "Review-gate configuration unavailable; verify the local plugin configuration") from None


@router.put("/configuration")
def put_configuration(update: Configuration) -> dict[str, Any]:
    try:
        available = profiles()
    except (OSError, ValueError, RuntimeError, ImportError):
        raise HTTPException(503, "Hermes profiles unavailable; verify the local Hermes profile configuration") from None
    try:
        valid = (update.implementation_profile in available and update.reviewer_profile in available
                 and profile_exists(update.implementation_profile) and profile_exists(update.reviewer_profile))
    except (OSError, ValueError, RuntimeError, ImportError):
        raise HTTPException(503, "Hermes profile validation unavailable; verify the local Hermes profile configuration") from None
    if not valid:
        raise HTTPException(409, "selected profile is missing; create it in Hermes before saving")
    try:
        with locked_state(write=True) as state:
            state["implementation_profile"] = update.implementation_profile
            state["reviewer_profile"] = update.reviewer_profile
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(503, "Review-gate configuration could not be saved; verify local plugin storage") from None
    # Existing enrollment bindings deliberately retain their original routing.
    return _view()


@router.post("/enroll")
def enroll(body: Enrollment) -> dict[str, Any]:
    try:
        task, runs, _events = _observe_task(body.board, body.task_id)
    except TelemetryUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if task is None:
        raise HTTPException(404, "Kanban task was not found on this board")
    try:
        binding = enroll_task(board=body.board, task=task, runs=runs)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result = _view()
    result["enrollment"] = binding
    return result


@router.get("/boards")
def boards() -> dict[str, Any]:
    """Return native board identities for the dashboard selector without switching boards."""
    try:
        from hermes_cli import kanban_db
        return {"boards": [entry.get("slug") for entry in kanban_db.list_boards() if isinstance(entry.get("slug"), str)]}
    except (OSError, ValueError, RuntimeError, ImportError):
        raise HTTPException(503, "Kanban board discovery is unavailable; verify local Kanban configuration") from None


@router.post("/board-activation")
def board_activation(body: BoardActivation) -> dict[str, Any]:
    """Publish board scope; native task state is never changed by activation."""
    try:
        policy = activate_board(body.board, activation_id=str(uuid.uuid4()))
        state = load_state()
        legacy_bound_ids = {binding["task_id"] for binding in state["tasks"].values()
                            if binding.get("board") == body.board}
        preview = _activation_preview(body.board, policy=policy, legacy_bound_ids=legacy_bound_ids)
    except TelemetryUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"policy": policy, "preview": preview}


@router.put("/recovery")
def recovery_control(body: RecoveryControl) -> dict[str, Any]:
    """Explicit opt-in/out only; it never changes native task state."""
    try:
        policy = set_recovery_policy(body.board, enabled=body.enabled, max_per_phase=body.max_per_phase)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"policy": policy, "message": "Recovery is enabled" if body.enabled else "Recovery is disabled"}


@router.put("/escalation")
def escalation_control(body: EscalationControl) -> dict[str, Any]:
    """Persist only future review-exhaustion routing; native state is untouched."""
    try:
        policy = set_escalation_policy(body.board, enabled=body.enabled,
                                       normal_correction_limit=body.normal_correction_limit,
                                       max_attempts=body.max_attempts,
                                       implementation_profile=body.implementation_profile,
                                       reviewer_profile=body.reviewer_profile)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"policy": policy,
            "message": "Review-correction escalation is enabled for future exhaustion" if body.enabled else "Review-correction escalation is disabled"}
