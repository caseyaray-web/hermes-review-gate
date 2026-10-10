"""Native Kanban review-gate tools and the normal worker completion guard."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .state import (MAX_CHANGES, authorize_recovery_run, bind_first_owned_run, board_name, board_policy,
                    current_escalation_attempt, escalation_entry, is_managed, pin_recovery_receipt, profile_exists, publish_runtime_escalation_routing,
                    reconcile_pending_escalation, recovery_budget_exhausted, recovery_entry, reserve_runtime_escalation, runtime_escalation_entry,
                    reserve_escalation, reserve_recovery, task_binding, terminalize_implementation_handoff_recovery,
                    terminalize_recovery, trusted_routing, update_recovery_identity, worker_profile)

_CONTEXT: Any | None = None
# Native reassignment can synchronously run the dispatch-tick hook before its
# API call returns. The outer reconciler holds the policy lock across that
# effect so policy disable cannot interleave with route publication. A nested
# reconciler would take a new flock descriptor and wait on the outer lock.
# This ContextVar fences only that synchronous call chain; it does not make the
# state lock reentrant or suppress another worker's later reconciliation.
_ACTIVE_ESCALATION_RECONCILIATION: ContextVar[tuple[str, str] | None] = ContextVar(
    "active_escalation_reconciliation", default=None)


@contextmanager
def _escalation_reconciliation_scope(board: str, task_id: str):
    token = _ACTIVE_ESCALATION_RECONCILIATION.set((board, task_id))
    try:
        yield
    finally:
        _ACTIVE_ESCALATION_RECONCILIATION.reset(token)


class GateError(ValueError):
    """A policy refusal that leaves native Kanban untouched."""


class EscalationPending(GateError):
    """A native effect may have landed; never send a second terminal effect."""


def _task_id() -> str:
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        raise GateError("review-gate tools are available only to a dispatcher-owned Kanban worker")
    return task_id


def _run_id() -> int:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    try:
        run_id = int(raw or "")
    except ValueError as exc:
        raise GateError("worker has no valid HERMES_KANBAN_RUN_ID; refusing lifecycle transition") from exc
    if run_id <= 0:
        raise GateError("worker has no valid HERMES_KANBAN_RUN_ID; refusing lifecycle transition")
    return run_id


def _context() -> Any:
    if _CONTEXT is None:
        raise GateError("review-gate plugin context is unavailable")
    return _CONTEXT


def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Use the supported PluginContext dispatch path, never the CLI or database."""
    raw = _context().dispatch_tool(tool_name, args)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as exc:
        raise GateError(f"native {tool_name} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise GateError(f"native {tool_name} returned an invalid response")
    if value.get("error"):
        raise GateError(f"native {tool_name} refused: {value['error']}")
    return value


def _show(task_id: str) -> dict[str, Any]:
    value = _dispatch("kanban_show", {"task_id": task_id})
    if not isinstance(value.get("task"), dict) or not isinstance(value.get("runs"), list) or not isinstance(value.get("events"), list):
        raise GateError("native kanban_show did not return task/runs/events")
    if len(value["events"]) >= 50:
        # kanban_show caps events, but phase and retry authority must not expire
        # after heartbeats/comments. Read the complete native snapshot without writes.
        from .native import snapshot
        return snapshot(board_name(), task_id)
    return value


def _binding(show: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Return a legacy binding or atomically pin an eligible board-policy run."""
    task_id = _task_id()
    board = board_name()
    binding = task_binding(task_id, board)
    if binding is None:
        profile = worker_profile()
        if not profile:
            raise GateError("worker profile is unavailable for board-policy binding")
        observed = show or _show(task_id)
        try:
            binding = bind_first_owned_run(board, observed["task"], observed["runs"], run_id=_run_id(), profile=profile)
        except ValueError as exc:
            raise GateError(f"board policy requires operator attention before review gating: {exc}") from exc
    if not all(profile_exists(binding[key]) for key in ("implementation_profile", "reviewer_profile")):
        raise GateError("a bound profile is missing or deleted; restore it before continuing")
    return task_id, binding


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return left == right


def _candidate(workspace: str) -> dict[str, Any]:
    """Bind approval to a committed, tracked-clean source candidate."""
    try:
        head = subprocess.run(["git", "-C", workspace, "rev-parse", "HEAD"], text=True,
                              capture_output=True, timeout=15, check=True).stdout.strip()
        status = subprocess.run(["git", "-C", workspace, "status", "--porcelain", "--untracked-files=all"],
                                text=True, capture_output=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"cannot bind a Git candidate for {workspace}: {exc}") from exc
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head.lower()):
        raise GateError("Git did not return a full HEAD revision")
    return {"head": head, "clean_tracked": not bool(status.strip())}


def _workspace_checkpoint(workspace: str, *, head: str | None = None, porcelain: str | None = None) -> dict[str, Any]:
    """Bounded failure checkpoint: names and digests only, never file contents."""
    root = Path(workspace).resolve(strict=True)
    if head is None:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True,
                              timeout=15, check=True).stdout.strip()
    if porcelain is None:
        porcelain = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                                   text=True, capture_output=True, timeout=15, check=True).stdout
    if len(head) != 40 or len(porcelain.encode()) > 65536:
        raise GateError("recovery checkpoint is invalid or too large")
    dirty: list[dict[str, str]] = []
    for line in porcelain.splitlines():
        if len(dirty) >= 128 or len(line) < 4 or line[2] != " ":
            raise GateError("recovery checkpoint refuses ambiguous or excessive Git status")
        name = line[3:]
        if not name or " -> " in name:
            raise GateError("recovery checkpoint refuses ambiguous Git paths")
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise GateError("recovery checkpoint refuses unsafe dirty paths")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        dirty.append({"path": name, "sha256": digest})
    return {"head": head, "dirty": dirty}


def _artifacts(workspace: str, paths: list[str]) -> list[dict[str, str]]:
    root = Path(workspace).resolve(strict=True)
    evidence = []
    if len(paths) > 20:
        raise GateError("at most 20 artifacts may accompany a handoff")
    for name in paths:
        path = Path(name)
        path = (root / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise GateError("artifacts must be regular workspace files no larger than 16 MiB")
        digest = hashlib.sha256()
        total = 0
        with path.open('rb') as stream:
            while chunk := stream.read(65536):
                total += len(chunk)
                if total > 16 * 1024 * 1024:
                    raise GateError("artifact grew beyond 16 MiB while reading")
                digest.update(chunk)
        evidence.append({"path": name, "sha256": digest.hexdigest()})
    return evidence


def _session() -> str:
    value = os.environ.get("HERMES_SESSION_ID")
    if not value:
        raise GateError("native worker session identity is unavailable")
    return value


def _arguments(args: dict, keys: set[str]) -> None:
    if not isinstance(args, dict) or set(args) - keys:
        raise GateError("unexpected review-gate arguments; identity and routing are code-owned")


def _event(events: list[dict[str, Any]], kind: str, run_id: int) -> dict[str, Any] | None:
    for event in reversed(events):
        if isinstance(event, dict) and event.get("kind") == kind and event.get("run_id") == run_id:
            return event
    return None


def _active_run(show: dict[str, Any], run_id: int, profile: str, *, review_claim: bool) -> dict[str, Any]:
    runs = [run for run in show["runs"] if isinstance(run, dict) and run.get("id") == run_id]
    if len(runs) != 1:
        raise GateError("native task has no exact current worker run")
    run = runs[0]
    if (show["task"].get("current_run_id") != run_id or run.get("status") != "running"
            or run.get("profile") != profile or run.get("ended_at") is not None):
        raise GateError("worker is not the active bound native run")
    claimed = _event(show["events"], "claimed", run_id)
    if claimed is None:
        raise GateError("native claim evidence is unavailable; do not infer phase from task membership")
    payload = claimed.get("payload")
    source_status = payload.get("source_status") if isinstance(payload, dict) else None
    if review_claim and source_status != "review":
        raise GateError("current native run was not claimed from review")
    if not review_claim and source_status == "review":
        raise GateError("reviewer run cannot finish implementation")
    return run


def _implementation_context() -> tuple[str, dict[str, Any], dict[str, Any], int, str]:
    task_id = _task_id()
    show = _show(task_id)
    task_id, binding = _binding(show)
    profile = worker_profile()
    route = trusted_routing(task_id, board_name(), binding, include_pending=True)
    expected_implementation = route["implementation_profile"]
    if not profile or profile != expected_implementation:
        raise GateError("only the bound implementation profile may finish this task")
    task = show["task"]
    if task.get("status") != "running" or task.get("assignee") != profile:
        raise GateError("task is not a running implementation assigned to this worker")
    workspace = task.get("workspace_path")
    if not isinstance(workspace, str) or not _same_path(workspace, binding["workspace_path"]):
        raise GateError("native task workspace no longer matches its enrollment")
    run_id = _run_id()
    _active_run(show, run_id, profile, review_claim=False)
    return task_id, binding, show, run_id, workspace


def _review_context() -> tuple[str, dict[str, Any], dict[str, Any], int, str, dict[str, Any]]:
    task_id = _task_id()
    show = _show(task_id)
    task_id, binding = _binding(show)
    profile = worker_profile()
    route = trusted_routing(task_id, board_name(), binding)
    if not profile or profile != route["reviewer_profile"]:
        raise GateError("only the bound reviewer may submit this verdict")

    task = show["task"]
    if task.get("status") != "running" or task.get("assignee") != profile:
        raise GateError("task is not a running review assigned to this worker")
    workspace = task.get("workspace_path")
    if not isinstance(workspace, str) or not _same_path(workspace, binding["workspace_path"]):
        raise GateError("native task workspace no longer matches its enrollment")
    reviewer_run_id = _run_id()
    _active_run(show, reviewer_run_id, profile, review_claim=True)
    review_events = [event for event in show["events"] if isinstance(event, dict) and event.get("kind") == "review_requested"]
    if not review_events:
        raise GateError("native task has no review handoff to judge")
    implementation_run_id = review_events[-1].get("run_id")
    implementation_run = next((run for run in show["runs"] if isinstance(run, dict) and run.get("id") == implementation_run_id), None)
    metadata = implementation_run.get("metadata") if implementation_run else None
    review = metadata.get("local_first_review") if isinstance(metadata, dict) else None
    implementation_profile = review.get("implementation_profile") if isinstance(review, dict) else None
    reviewer_profile = review.get("reviewer_profile") if isinstance(review, dict) else None
    if (type(implementation_run_id) is not int or not implementation_run or implementation_run.get("profile") != implementation_profile
            or implementation_run.get("status") != "review" or implementation_run.get("outcome") != "review_requested"
            or implementation_run.get("ended_at") is None or implementation_run_id >= reviewer_run_id):
        raise GateError("review handoff is not an ended implementation run preceding this reviewer")
    if not isinstance(review, dict) or review.get("implementation_run_id") != implementation_run_id:
        raise GateError("review handoff lacks a bound implementation candidate")
    implementation_profile = review.get("implementation_profile") if isinstance(review, dict) else None
    reviewer_profile = review.get("reviewer_profile") if isinstance(review, dict) else None
    if (not isinstance(implementation_profile, str) or not isinstance(reviewer_profile, str)
            or implementation_profile != route["implementation_profile"]
            or reviewer_profile != route["reviewer_profile"]
            or reviewer_profile != profile):
        raise GateError("review handoff routing does not match this enrollment")
    if not metadata.get("worker_session_id") or metadata["worker_session_id"] == _session():
        raise GateError("review requires a distinct native worker session")
    candidate = review.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("clean_tracked") is not True:
        raise GateError("review handoff candidate is incomplete or not clean")
    if _candidate(workspace) != candidate:
        raise GateError("source candidate changed after implementation; approval is stale")
    if _artifacts(workspace, review.get("artifacts", [])) != review.get("artifact_evidence", []):
        raise GateError("implementation artifacts changed after handoff; approval is stale")
    return task_id, binding, show, reviewer_run_id, workspace, review


def _result(value: dict[str, Any], **extra: Any) -> str:
    return json.dumps({**value, **extra})


def _reconcile_handoff(task_id: str, implementation_run_id: int) -> dict[str, Any] | None:
    show = _show(task_id)
    task = show["task"]
    run = next((r for r in show["runs"] if r.get("id") == implementation_run_id), {})
    if (run.get("outcome") == "review_requested" and run.get("ended_at") is not None
            and _event(show["events"], "review_requested", implementation_run_id)):
        return {"ok": True, "task_id": task_id, "status": task.get("status"), "reconciled": True}
    return None


def _terminalize_recovered_implementation_handoff(task_id: str, implementation_run_id: int) -> bool:
    """Release only the exact recovery lease after native confirms its handoff."""
    if not _reconcile_handoff(task_id, implementation_run_id):
        raise GateError("native review handoff could not be verified; recovery lease remains active")
    try:
        return terminalize_implementation_handoff_recovery(board_name(), task_id, implementation_run_id)
    except ValueError as exc:
        raise GateError(f"native handoff landed but implementation recovery finalization was refused: {exc}") from exc


def _reconcile_ended_implementation_handoff_for_reviewer(task_id: str, board: str, entry: dict[str, Any], *,
                                                          run_id: int, profile: str) -> bool:
    """Close the exact prior implementation lease when a reviewer won the race.

    This is intentionally narrow: only an active implementation recovery whose
    authorized and receipted run is the ended native review handoff can yield to
    a distinct currently-running reviewer claimed from ``review``.  Any other
    record remains a recovery admission and fails closed through its normal path.
    """
    if entry.get("phase") != "implementation":
        return False
    implementation_run_id = entry.get("authorized_run_id")
    if implementation_run_id == run_id:
        return False
    receipt = entry.get("receipt")
    if type(implementation_run_id) is not int or not isinstance(receipt, dict) or receipt.get("run_id") != implementation_run_id:
        raise GateError("implementation recovery handoff receipt is incomplete")
    show = _show(task_id)
    binding = task_binding(task_id, board)
    route = trusted_routing(task_id, board, binding) if binding else None
    if not binding or not route or profile != route.get("reviewer_profile"):
        raise GateError("implementation recovery cannot yield to an unbound reviewer")
    if run_id == implementation_run_id:
        return False
    _active_run(show, run_id, profile, review_claim=True)
    implementation = next((run for run in show["runs"] if isinstance(run, dict) and run.get("id") == implementation_run_id), None)
    metadata = implementation.get("metadata") if isinstance(implementation, dict) else None
    handoff = metadata.get("local_first_review") if isinstance(metadata, dict) else None
    if (not isinstance(implementation, dict) or implementation.get("profile") != route.get("implementation_profile")
            or implementation.get("status") != "review" or implementation.get("outcome") != "review_requested"
            or implementation.get("ended_at") is None or not _event(show["events"], "review_requested", implementation_run_id)
            or not isinstance(handoff, dict) or handoff.get("implementation_run_id") != implementation_run_id
            or handoff.get("implementation_profile") != route.get("implementation_profile")
            or handoff.get("reviewer_profile") != profile):
        raise GateError("implementation recovery handoff provenance is not the exact native review transition")
    try:
        if not terminalize_implementation_handoff_recovery(board, task_id, implementation_run_id):
            raise GateError("implementation recovery disappeared before reviewer reconciliation")
    except ValueError as exc:
        raise GateError(f"implementation recovery reviewer reconciliation was refused: {exc}") from exc
    return True


def finish_implementation(args: dict[str, Any], **_: Any) -> str:
    _arguments(args, {"summary", "artifacts"})
    _session()
    task_id, binding, _show_before, run_id, workspace = _implementation_context()
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 16000:
        raise GateError("summary must be substantive text of at most 16000 characters")
    summary = summary.strip()
    artifacts = args.get("artifacts") or []
    if not summary:
        raise GateError("summary is required")
    if not isinstance(artifacts, list) or not all(isinstance(item, str) and item.strip() for item in artifacts):
        raise GateError("artifacts must be a list of non-empty paths")
    candidate = _candidate(workspace)
    if not candidate["clean_tracked"]:
        raise GateError("implementation must have a clean tracked Git candidate before review")
    metadata = {"local_first_review": {
        "implementation_profile": worker_profile(), "reviewer_profile": trusted_routing(task_id, board_name(), binding)["reviewer_profile"],
        "implementation_run_id": run_id, "candidate": candidate, "artifacts": artifacts,
        "artifact_evidence": _artifacts(workspace, artifacts),
        "binding": {field: binding[field] for field in ("board", "task_id", "implementation_profile", "reviewer_profile", "workspace_path", "policy_activation_id", "policy_native_run_watermark", "native_run_id") if field in binding},
    }}
    try:
        value = _dispatch("kanban_request_review", {"summary": summary, "reviewer": metadata["local_first_review"]["reviewer_profile"],
                                                      "metadata": metadata, "artifacts": artifacts})
    except Exception as exc:
        landed = _reconcile_handoff(task_id, run_id)
        if landed:
            _terminalize_recovered_implementation_handoff(task_id, run_id)
            return _result(landed, message="Review handoff already landed; stop implementation work.")
        raise GateError(f"review handoff outcome is unknown; no automatic retry was sent; inspect the native task/run before retrying: {exc}") from exc
    observed = _reconcile_handoff(task_id, run_id)
    if not observed:
        raise GateError("native handoff could not be verified; inspect the native run before retrying")
    _terminalize_recovered_implementation_handoff(task_id, run_id)
    return _result(value, message="Review requested. Stop work; the configured reviewer owns the next transition.")


def _changes_count(runs: list[dict[str, Any]]) -> int:
    return sum(1 for run in runs if isinstance(run, dict) and run.get("outcome") == "changes_requested")


def _pending_escalation_proof(task_id: str, board: str, entry: dict[str, Any], *,
                              required_run_id: int | None = None, required_profile: str | None = None) -> bool:
    """Accept only the exact target's first post-intent native claim.

    This is intentionally shared by synchronous submission, watchdog recovery,
    and pre-tool admission.  A running task is never sufficient on its own.
    """
    binding = task_binding(task_id, board)
    attempt = current_escalation_attempt(entry)
    intent = attempt.get("intent", {})
    target = attempt.get("implementation_profile")
    target_reviewer = attempt.get("reviewer_profile")
    origin = attempt.get("origin")
    review_run_id = intent.get("review_run_id")
    if (not isinstance(binding, dict) or entry.get("binding") != binding or not isinstance(target, str)
            or not isinstance(target_reviewer, str) or not isinstance(origin, dict)
            or not isinstance(review_run_id, int)):
        return False
    observed = _show(task_id); task = observed["task"]
    reviewer = next((run for run in observed["runs"] if run.get("id") == review_run_id), None)
    if (task.get("id") != task_id or not _event(observed["events"], "changes_requested", review_run_id)
            or not isinstance(reviewer, dict) or reviewer.get("profile") != origin.get("reviewer_profile")
            or reviewer.get("outcome") != "changes_requested" or reviewer.get("ended_at") is None):
        return False
    handoff_event = next((event for event in reversed(observed["events"])
                          if event.get("kind") == "review_requested" and isinstance(event.get("run_id"), int)
                          and event.get("run_id") < review_run_id), None)
    handoff = next((run for run in observed["runs"] if handoff_event and run.get("id") == handoff_event.get("run_id")), None)
    metadata = handoff.get("metadata") if isinstance(handoff, dict) else None
    packet = metadata.get("local_first_review") if isinstance(metadata, dict) else None
    expected_binding = {field: binding[field] for field in ("board", "task_id", "implementation_profile", "reviewer_profile", "workspace_path", "policy_activation_id", "policy_native_run_watermark", "native_run_id") if field in binding}
    if (not isinstance(packet, dict) or packet.get("candidate") != intent.get("candidate")
            or packet.get("implementation_profile") != origin.get("implementation_profile")
            or packet.get("reviewer_profile") != origin.get("reviewer_profile")
            or packet.get("binding") != expected_binding):
        return False
    if task.get("status") in {"ready", "todo"}:
        if task.get("assignee") != target:
            try:
                from .native import reassign_ready_task
                if not reassign_ready_task(board, task_id, target):
                    return False
            except Exception:
                return False
            observed = _show(task_id); task = observed["task"]
        return task.get("assignee") == target and task.get("status") in {"ready", "todo"}
    if task.get("status") != "running" or task.get("assignee") != target:
        return False
    run_id = task.get("current_run_id")
    active = next((run for run in observed["runs"] if run.get("id") == run_id), None)
    claimed = _event(observed["events"], "claimed", run_id) if isinstance(run_id, int) else None
    payload = claimed.get("payload") if isinstance(claimed, dict) else None
    source = payload.get("source_status") if isinstance(payload, dict) else None
    if (not isinstance(active, dict) or active.get("profile") != target or active.get("status") != "running"
            or active.get("ended_at") is not None or claimed is None or source == "review"):
        return False
    return ((required_run_id is None or run_id == required_run_id)
            and (required_profile is None or required_profile == target))


def _reconcile_pending_escalation(task_id: str, board: str, *, required_run_id: int | None = None,
                                  required_profile: str | None = None) -> str:
    entry = escalation_entry(task_id, board)
    binding = task_binding(task_id, board)
    if not entry or not binding:
        return "pending"
    attempt = current_escalation_attempt(entry)
    if not isinstance(attempt.get("intent", {}).get("review_run_id"), int):
        return "pending"
    with _escalation_reconciliation_scope(board, task_id):
        return reconcile_pending_escalation(board, task_id, binding=binding,
            review_run_id=attempt["intent"]["review_run_id"],
            reconcile=lambda sealed: _pending_escalation_proof(task_id, board, sealed,
                required_run_id=required_run_id, required_profile=required_profile))


def _escalate_after_exhaustion(task_id: str, binding: dict[str, Any], show: dict[str, Any], reviewer_run_id: int,
                               review: dict[str, Any], rationale: str) -> dict[str, Any]:
    reserve_escalation(board_name(), task_id, review_run_id=reviewer_run_id, binding=binding,
                       candidate=review["candidate"], change_count=_changes_count(show["runs"]))
    try:
        value = _dispatch("kanban_request_changes", {"reason": rationale})
    except Exception as exc:
        if not _event(_show(task_id)["events"], "changes_requested", reviewer_run_id):
            raise EscalationPending(f"escalation changes outcome is unknown; no retry was sent: {exc}") from exc
        value = {"ok": True, "task_id": task_id, "reconciled": True}
    status = _reconcile_pending_escalation(task_id, board_name())
    if status != "routed":
        raise EscalationPending("escalation routing remains pending or held; no retry was sent")
    return value


def submit_review(args: dict[str, Any], **_: Any) -> str:
    _arguments(args, {"verdict", "rationale"})
    _session()
    verdict = args.get("verdict")
    rationale = args.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 16000:
        raise GateError("rationale must be substantive text of at most 16000 characters")
    rationale = rationale.strip()
    if verdict not in {"approved", "changes_requested"} or not rationale:
        raise GateError("verdict must be approved or changes_requested and rationale is required")
    task_id, _binding_value, show, reviewer_run_id, _workspace, _review = _review_context()
    if verdict == "approved":
        tool, native_args = "kanban_complete", {"summary": rationale, "metadata": {"local_first_review": {
            "verdict": "approved", "rationale": rationale, "candidate": _review["candidate"],
            "implementation_run_id": _review["implementation_run_id"],
            "reviewer_run_id": reviewer_run_id, "reviewer_profile": worker_profile(),
        }}}
        expected_status, expected_event = "done", "completed"
    elif _changes_count(show["runs"]) >= (board_policy(board_name()) or {}).get("escalation", {}).get("normal_correction_limit", MAX_CHANGES):
        policy = board_policy(board_name()) or {}
        escalation = policy.get("escalation", {})
        if escalation.get("enabled") is True:
            try:
                value = _escalate_after_exhaustion(task_id, _binding_value, show, reviewer_run_id, _review, rationale)
            except EscalationPending:
                # request_changes may already have ended this reviewer. Never
                # issue kanban_block from a stale run; retain the durable intent
                # for exact native/operator reconciliation instead.
                raise
            except (ValueError, GateError) as exc:
                tool, native_args = "kanban_block", {"reason": "Review escalation exhausted or refused; operator attention required. Latest findings: " + rationale, "kind": "needs_input"}
                expected_status, expected_event = "blocked", None
            else:
                return _result(value, verdict=verdict, escalated=True)
        else:
            tool, native_args = "kanban_block", {"reason": "Review changes exhausted; operator attention required. Latest findings: " + rationale, "kind": "needs_input"}
            expected_status, expected_event = "blocked", None
    else:
        tool, native_args = "kanban_request_changes", {"reason": rationale}
        expected_status, expected_event = "ready", "changes_requested"
    try:
        value = _dispatch(tool, native_args)
    except Exception as exc:
        observed = _show(task_id)
        task = observed["task"]
        landed = task.get("status") == expected_status and (expected_event is None or _event(observed["events"], expected_event, reviewer_run_id))
        if landed:
            return _result({"ok": True, "task_id": task_id, "status": expected_status}, verdict=verdict, reconciled=True)
        raise GateError(f"review verdict outcome is unknown; no automatic retry was sent; inspect the native task/run before retrying: {exc}") from exc
    observed = _show(task_id)
    ended = next((r for r in observed["runs"] if r.get("id") == reviewer_run_id), {})
    if ended.get("ended_at") is None or (expected_event and not _event(observed["events"], expected_event, reviewer_run_id)):
        raise GateError("verdict native run could not be verified; inspect before retrying")
    return _result(value, verdict=verdict)


def _failed_phase(show: dict[str, Any], failed: dict[str, Any]) -> str | None:
    """Resolve failure phase from exact native claim or terminal retry evidence."""
    run_id = failed.get("id")
    if type(run_id) is not int:
        return None
    claimed = _event(show["events"], "claimed", run_id)
    payload = claimed.get("payload") if claimed else None
    source = payload.get("source_status") if isinstance(payload, dict) else None
    if source == "review":
        return "review"
    if source in {"ready", "todo"}:
        return "implementation"
    # Native ready claims deliberately omit source_status.  A terminal
    # dispatcher event for the *same run* persists retry_status after resolving
    # the claim provenance; use that exact producer evidence rather than
    # inferring phase from the currently blocked task.
    outcome = failed.get("outcome")
    terminal = _event(show["events"], outcome, run_id) if isinstance(outcome, str) else None
    retry_payload = terminal.get("payload") if terminal else None
    retry_status = retry_payload.get("retry_status") if isinstance(retry_payload, dict) else None
    return "review" if retry_status == "review" else "implementation" if retry_status in {"ready", "todo"} else None


def _allowed_terminal_failure(show: dict[str, Any], failed: dict[str, Any]) -> bool:
    """Only recover the exact started-worker iteration-exhaustion failure.

    A ``needs_input`` hold is an operator decision, not failure evidence.  In
    particular, native repeated-hold policy may surface it as ``triage`` rather
    than ``blocked``.  A ``spawn_failed`` record proves no worker ran, so it is
    deliberately not a substitute for the RM02-class exhausted worker.
    """
    run_id = failed.get("id")
    if failed.get("outcome") != "gave_up" or type(run_id) is not int:
        return False
    event = _event(show["events"], "gave_up", run_id)
    payload = event.get("payload") if event else None
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    started = _event(show["events"], "spawned", run_id)
    return (isinstance(started, dict)
            and payload.get("trigger_outcome") == "timed_out"
            and type(payload.get("effective_limit")) is int
            and payload.get("effective_limit") == 1
            and payload.get("retry_status") in {"ready", "review"}
            and type(payload.get("budget_used")) is int
            and type(payload.get("budget_max")) is int
            and payload.get("budget_used") == payload.get("budget_max")
            and payload["budget_used"] > 0
            and isinstance(error, str)
            and error.startswith("Iteration budget exhausted ("))


def _first_unbound_gave_up(policy: dict[str, Any], show: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The sole historical adoption rule: exact first post-watermark gave_up."""
    task, runs = show["task"], show["runs"]
    if task.get("status") != "blocked" or len(runs) != 1:
        return None
    failed = runs[0]
    if (not _allowed_terminal_failure(show, failed) or type(failed.get("id")) is not int
            or failed["id"] <= policy["native_run_watermark"] or failed.get("profile") != policy["implementation_profile"]):
        return None
    event = _event(show["events"], "gave_up", failed["id"])
    payload = event.get("payload") if event else None
    if not isinstance(payload, dict) or payload.get("effective_limit") != 1:
        return None
    phase = _failed_phase(show, failed)
    return (failed, phase) if phase == "implementation" else None


def _workspace_is_exclusive(board: str, task_id: str, workspace: str) -> bool:
    from .native import board_snapshot
    for other in board_snapshot(board):
        if other.get("id") != task_id and other.get("status") in {"running", "review"} and isinstance(other.get("workspace_path"), str) and _same_path(other["workspace_path"], workspace):
            return False
    return True


def _persisted_review_handoff(show: dict[str, Any], run_id: int, binding: dict[str, Any]) -> None:
    """Require the original ended implementation handoff for a recovered reviewer."""
    events = [event for event in show["events"] if isinstance(event, dict) and event.get("kind") == "review_requested"]
    if not events:
        raise GateError("recovered reviewer has no persisted original handoff")
    implementation_run_id = events[-1].get("run_id")
    implementation = next((item for item in show["runs"] if item.get("id") == implementation_run_id), None)
    metadata = implementation.get("metadata") if isinstance(implementation, dict) else None
    handoff = metadata.get("local_first_review") if isinstance(metadata, dict) else None
    route = trusted_routing(binding["task_id"], binding["board"], binding)
    if (type(implementation_run_id) is not int or implementation_run_id >= run_id
            or not isinstance(implementation, dict) or implementation.get("profile") != route["implementation_profile"]
            or implementation.get("outcome") != "review_requested" or implementation.get("status") != "review"
            or implementation.get("ended_at") is None or not isinstance(handoff, dict)
            or handoff.get("implementation_run_id") != implementation_run_id
            or handoff.get("implementation_profile") != route["implementation_profile"]
            or handoff.get("reviewer_profile") != route["reviewer_profile"]):
        raise GateError("recovered reviewer handoff provenance is invalid")


def _reconcile_recovery_claim(task_id: str, board: str, entry: dict[str, Any], *, run_id: int | None = None,
                              profile: str | None = None) -> dict[str, Any]:
    """One exact native admission path for hooks, ticks, and first model tools.

    ``claim_review_task`` has no claimed hook.  Therefore the pre-tool path is
    authoritative: it reads the current native run and pins authorization only
    after phase and original-handoff provenance agree with the durable intent.
    """
    if entry.get("intent", {}).get("status") not in {"unblock_requested", "unblock_verified", "native_resume_observed"}:
        raise GateError("recovery has no resumable unblock intent")
    show = _show(task_id)
    task = show["task"]
    observed_run_id = task.get("current_run_id")
    if run_id is not None and observed_run_id != run_id:
        raise GateError("claimed hook run is no longer the native current run")
    if type(observed_run_id) is not int or task.get("status") != "running":
        raise GateError("native recovery replacement is not running")
    run = next((item for item in show["runs"] if item.get("id") == observed_run_id), None)
    binding = task_binding(task_id, board)
    route = trusted_routing(task_id, board, binding) if binding else None
    expected_profile = route.get("reviewer_profile") if entry.get("phase") == "review" and route else route.get("implementation_profile") if route else None
    if (not isinstance(run, dict) or not isinstance(expected_profile, str) or task.get("assignee") != expected_profile
            or run.get("profile") != expected_profile or run.get("status") != "running" or run.get("ended_at") is not None
            or profile is not None and profile != expected_profile):
        raise GateError("native recovery replacement profile is not the intended assignee")
    claim = _event(show["events"], "claimed", observed_run_id)
    payload = claim.get("payload") if claim else None
    source = payload.get("source_status") if isinstance(payload, dict) else None
    # Native ready claims omit source_status; that omission is admissible only
    # for implementation and only after the durable unblock intent names ready.
    if source is None and entry.get("phase") == "implementation":
        source = "ready"
    if source != entry.get("expected_source_status") or source not in {"ready", "review"}:
        raise GateError("replacement claim phase does not match recovery intent")
    binding = task_binding(task_id, board)
    if not binding or not profile_exists(expected_profile):
        raise GateError("bound recovery profile is unavailable or drifted")
    if entry.get("phase") == "review":
        _persisted_review_handoff(show, observed_run_id, binding)
    workspace = task.get("workspace_path")
    if (not isinstance(workspace, str) or not _same_path(workspace, binding["workspace_path"])
            or not _same_path(workspace, entry["workspace_path"])):
        raise GateError("native recovery workspace drifted")
    # Once an exact running claim is observed, recover from a lost unblock
    # response without sending another unblock.  This write is idempotent.
    if entry.get("intent", {}).get("status") == "unblock_requested":
        entry = update_recovery_identity(board, task_id, entry["failed_run_id"], entry["phase"],
                                         intent={"status": "unblock_verified", "native_status": source})
    if entry.get("authorized_run_id") not in {None, observed_run_id}:
        raise GateError("a different recovery run is already authorized")
    authorize_recovery_run(board, task_id, entry["failed_run_id"], entry["phase"], observed_run_id, expected_profile)
    refreshed = recovery_entry(task_id, board, failed_run_id=entry["failed_run_id"], phase=entry["phase"])
    if not refreshed or refreshed.get("authorized_run_id") != observed_run_id or refreshed.get("authorized_profile") != expected_profile:
        raise GateError("recovery authorization was not durably bound to this run")
    return refreshed


def _reserve_claimed_recovery(task_id: str, board: str, *, run_id: int, profile: str) -> dict[str, Any] | None:
    """Reserve the exact exhausted predecessor for a claim native already resumed.

    Native dispatch invokes its tick observer only after it has promoted and
    claimed a tripped review task.  Review claims deliberately have no claimed
    hook, so this is the first supported, pre-model-tool observation point.  It
    records that native resumed the task; it never fabricates an unblock call.
    """
    policy = board_policy(board)
    if not policy or policy.get("recovery", {}).get("enabled") is not True:
        return None
    show = _show(task_id)
    task, runs = show["task"], show["runs"]
    if task.get("status") != "running" or task.get("current_run_id") != run_id or len(runs) < 2:
        return None
    failed = runs[-2]
    if not isinstance(failed, dict) or not _allowed_terminal_failure(show, failed):
        return None
    phase = _failed_phase(show, failed)
    binding = task_binding(task_id, board)
    workspace = task.get("workspace_path")
    route = trusted_routing(task_id, board, binding) if binding else None
    expected_profile = route.get("reviewer_profile") if phase == "review" and route else route.get("implementation_profile") if route else None
    if (phase not in {"implementation", "review"} or not binding or profile != expected_profile
            or task.get("assignee") != expected_profile or not isinstance(workspace, str)
            or not _same_path(workspace, binding["workspace_path"]) or not profile_exists(profile)
            or not _workspace_is_exclusive(board, task_id, workspace)):
        return None
    entry = reserve_recovery(board, task_id, failed_run_id=failed["id"], phase=phase,
                             workspace_path=workspace, checkpoint=_workspace_checkpoint(workspace),
                             binding=binding, adopted=False)
    return update_recovery_identity(board, task_id, failed["id"], phase,
                                    intent={"status": "native_resume_observed", "native_status": "review" if phase == "review" else "ready"})


def _terminal_unadmitted_replacement(show: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Recognize one ended direct successor without admitting a model tool.

    A recovery reservation may survive long enough for native to claim and end
    its replacement before the worker invokes a first tool.  That replacement
    never has an ``authorized_run_id`` or receipt, but it can still be tied to
    this recovery by one newer run, its exact claim phase, bound profile, and a
    terminal task with no live runs.  Anything less is ambiguous and retains
    the lease for operator inspection.
    """
    if entry.get("authorized_run_id") is not None or entry.get("receipt") is not None:
        return False
    failed_run_id = entry.get("failed_run_id")
    phase = entry.get("phase")
    if type(failed_run_id) is not int or phase not in {"implementation", "review"}:
        return False
    task = show.get("task")
    runs = show.get("runs")
    if not isinstance(task, dict) or not isinstance(runs, list) or task.get("status") not in {"done", "blocked", "triage", "failed"}:
        return False
    successors = [run for run in runs if isinstance(run, dict) and type(run.get("id")) is int and run["id"] > failed_run_id]
    if len(successors) != 1 or any(isinstance(run, dict) and run.get("status") == "running" for run in runs):
        return False
    replacement = successors[0]
    board = entry.get("board")
    task_id = entry.get("task_id")
    binding = task_binding(task_id, board) if isinstance(board, str) and isinstance(task_id, str) else None
    route = trusted_routing(task_id, board, binding) if binding and isinstance(board, str) and isinstance(task_id, str) else None
    expected_profile = route.get("reviewer_profile") if phase == "review" and route else route.get("implementation_profile") if route else None
    if (not isinstance(expected_profile, str) or task.get("current_run_id") != replacement.get("id")
            or task.get("assignee") != expected_profile or replacement.get("profile") != expected_profile
            or replacement.get("status") == "running" or replacement.get("ended_at") is None):
        return False
    claim = _event(show["events"], "claimed", replacement["id"])
    payload = claim.get("payload") if claim else None
    source = payload.get("source_status") if isinstance(payload, dict) else None
    if source is None and phase == "implementation":
        source = "ready"
    return source == entry.get("expected_source_status") == ("review" if phase == "review" else "ready")


def _recovery_guard(tool_name: str) -> dict[str, str] | None:
    """Fail closed and pin one receipt before a recovered worker's first tool."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return None
    try:
        entry = recovery_entry(task_id, board_name())
    except Exception as exc:
        return {"action": "block", "message": f"Recovery state is unreadable; refusing tool: {exc}"}
    if entry is None:
        # Direct lifecycle calls remain on their ordinary board-policy path.  A
        # recovered worker is admitted by its first normal tool; lifecycle calls
        # are independently refused below and therefore cannot bypass recovery.
        if tool_name in {"kanban_complete", "kanban_request_review", "kanban_request_changes"}:
            return None
        # Ordinary tool tests can carry a task id without an actual worker run.
        # They are not recovery candidates; preserve normal handling instead of
        # converting malformed context into a recovery-state error.
        try:
            run_id, profile = _run_id(), worker_profile()
        except GateError:
            return None
        try:
            if profile:
                entry = _reserve_claimed_recovery(task_id, board_name(), run_id=run_id, profile=profile)
        except Exception as exc:
            return {"action": "block", "message": f"Recovery state is unreadable; refusing tool: {exc}"}
        if entry is None:
            return None
    if not entry:
        return None
    try:
        run_id, profile = _run_id(), worker_profile()
        if entry.get("receipt", {}).get("run_id") == run_id:
            return None
        if not profile:
            raise GateError("recovery worker profile is unavailable")
        if _reconcile_ended_implementation_handoff_for_reviewer(task_id, board_name(), entry,
                                                                run_id=run_id, profile=profile):
            return None
        entry = _reconcile_recovery_claim(task_id, board_name(), entry, run_id=run_id, profile=profile)
        if _workspace_checkpoint(entry["workspace_path"]) != entry.get("checkpoint"):
            raise GateError("recovery workspace changed before admission")
        pin_recovery_receipt(board_name(), task_id, entry["failed_run_id"], entry["phase"], run_id,
                            {"tool": tool_name, "profile": profile})
    except Exception as exc:
        return {"action": "block", "message": f"Recovery admission refused: {exc}"}
    return None


def watchdog_claimed(*, task_id: str, board: str, assignee: str | None = None, run_id: int | None = None,
                     profile_name: str | None = None, **_: Any) -> None:
    # This hook is only an optimization for ready claims.  Native review claims
    # do not fire it, so pre-tool admission below is authoritative for review.
    if type(run_id) is not int or not isinstance(assignee, str) or not assignee:
        return
    try:
        entry = recovery_entry(task_id, board)
        if entry and entry.get("receipt", {}).get("run_id") != run_id:
            _reconcile_recovery_claim(task_id, board, entry, run_id=run_id, profile=assignee)
    except Exception:
        return


def _reconcile_runtime_exhaustion_escalation(task_id: str, board: str, entry: dict[str, Any]) -> bool:
    """Resume one exact blocked exhausted task, then route its existing worktree."""
    binding = task_binding(task_id, board)
    if entry.get("intent", {}).get("status") != "unblock_requested":
        return False
    attempt = entry.get("attempts", [{}])[-1]
    target = attempt.get("implementation_profile") if isinstance(attempt, dict) else None
    if not isinstance(binding, dict) or entry.get("binding") != binding or not isinstance(target, str) or not profile_exists(target):
        return False
    observed = _show(task_id)
    task = observed.get("task", {})
    if task.get("status") == "blocked":
        value = _dispatch("kanban_unblock", {"task_id": task_id})
        if value.get("status") not in {"ready", "todo"}:
            return False
        observed = _show(task_id); task = observed.get("task", {})
    if task.get("status") not in {"ready", "todo"}:
        return False
    if task.get("assignee") != target:
        from .native import reassign_ready_task
        if not reassign_ready_task(board, task_id, target):
            return False
        observed = _show(task_id); task = observed.get("task", {})
    if task.get("status") not in {"ready", "todo"} or task.get("assignee") != target:
        return False
    publish_runtime_escalation_routing(board, task_id, binding=binding)
    return True


def watchdog_tick(*, board: str | None = None, dry_run: bool = False, **_: Any) -> None:
    """Autonomous post-dispatch recovery; all uncertain observations fail closed."""
    if dry_run or not board: return
    # A native reassignment may synchronously emit this hook while the outer
    # reconciler owns the escalation policy lock. Do not recurse into another
    # autonomous reconciliation in that same call chain; the outer readback
    # decides the pending/routed outcome after native returns.
    active = _ACTIVE_ESCALATION_RECONCILIATION.get()
    if active is not None and active[0] == board:
        return
    try:
        # Reconcile only a durable correction-escalation intent whose exact
        # reviewer transition is already visible. This is separate from runtime
        # recovery policy and never scans/revives old operator holds.
        from .state import load_state
        for entry in load_state().get("escalations", {}).values():
            attempt = current_escalation_attempt(entry)
            if entry.get("board") != board or attempt.get("intent", {}).get("status") != "changes_requested_pending":
                continue
            task_id = entry.get("task_id")
            review_run_id = attempt.get("intent", {}).get("review_run_id")
            if not isinstance(task_id, str) or type(review_run_id) is not int:
                continue
            status = _reconcile_pending_escalation(task_id, board)
            if status == "held":
                continue
        # Runtime exhaustion is separate from review-correction escalation.  It
        # only revisits a task-scoped intent created from an exact eligible run.
        for entry in load_state().get("runtime_escalations", {}).values():
            if entry.get("board") == board and entry.get("intent", {}).get("status") != "routed":
                task_id = entry.get("task_id")
                if isinstance(task_id, str):
                    _reconcile_runtime_exhaustion_escalation(task_id, board, entry)
        policy = board_policy(board)
        if not policy or policy.get("recovery", {}).get("enabled") is not True: return
        candidates = list(load_state()["tasks"].values())
        # Historical adoption is an ambiguity-refusal boundary, not a per-tick
        # throttle.  Read every blocked unbound card completely before choosing:
        # if two exact RM02 shapes remain, neither is safe to adopt and an
        # operator must resolve the history.  Bound recovery candidates stay in
        # ``candidates`` and continue through their ordinary reconciliation.
        from .native import board_snapshot
        known = {b["task_id"] for b in candidates if b.get("board") == board}
        unbound_matches: list[str] = []
        unbound_uncertain = False
        for native_task in board_snapshot(board):
            task_id = native_task.get("id")
            if task_id in known or native_task.get("status") != "blocked":
                continue
            if not isinstance(task_id, str) or not task_id:
                unbound_uncertain = True
                continue
            try:
                if _first_unbound_gave_up(policy, _show(task_id)) is not None:
                    unbound_matches.append(task_id)
            except Exception:
                unbound_uncertain = True
        if not unbound_uncertain and len(unbound_matches) == 1:
            candidates.append({"board": board, "task_id": unbound_matches[0], "_unbound": True})
    except Exception:
        return
    for candidate in candidates:
        if candidate.get("board") != board or not isinstance(candidate.get("task_id"), str): continue
        task_id = candidate["task_id"]
        try:
            show = _show(task_id); task, runs = show["task"], show["runs"]
            existing = recovery_entry(task_id, board)
            if existing:
                # A claim can commit after our unblock write but before its
                # response/readback returns.  Reconcile exact claim evidence
                # first; never resend unblock for a durable intent.
                if task.get("status") == "running":
                    _reconcile_recovery_claim(task_id, board, existing)
                elif existing.get("intent", {}).get("status") == "unblock_requested" and task.get("status") in {"ready", "todo", "review"}:
                    update_recovery_identity(board, task_id, existing["failed_run_id"], existing["phase"], intent={"status": "unblock_verified", "native_status": task.get("status")})
                elif existing.get("authorized_run_id") and task.get("status") in {"done", "blocked", "triage", "failed"}:
                    terminalize_recovery(board, task_id, existing["failed_run_id"], existing["phase"], "native_terminal")
                elif _terminal_unadmitted_replacement(show, existing):
                    terminalize_recovery(board, task_id, existing["failed_run_id"], existing["phase"],
                                         "replacement_terminal_before_admission")
                continue
            if task.get("status") != "blocked" or not runs: continue
            failed = runs[-1]; phase = _failed_phase(show, failed)
            if not _allowed_terminal_failure(show, failed) or phase is None: continue
            binding = task_binding(task_id, board)
            adopted = False
            if binding is None:
                exact = _first_unbound_gave_up(policy, show)
                if exact is None: continue
                failed, phase = exact; adopted = True
                workspace = task.get("workspace_path")
                if not isinstance(workspace, str) or task.get("workspace_kind") != "worktree" or not Path(workspace).is_absolute(): continue
                binding = {"board": board, "task_id": task_id, "implementation_profile": policy["implementation_profile"], "reviewer_profile": policy["reviewer_profile"], "workspace_path": workspace, "policy_activation_id": policy["activation_id"], "policy_native_run_watermark": policy["native_run_watermark"], "native_run_id": failed["id"]}
            workspace = task.get("workspace_path")
            route = trusted_routing(task_id, board, binding)
            profile = route["reviewer_profile"] if phase == "review" else route["implementation_profile"]
            if not isinstance(workspace, str) or not _same_path(workspace, binding["workspace_path"]) or task.get("assignee") != profile or not profile_exists(profile) or not _workspace_is_exclusive(board, task_id, workspace): continue
            checkpoint = _workspace_checkpoint(workspace)
            runtime_policy = policy.get("runtime_escalation", {})
            if (phase == "implementation" and runtime_policy.get("enabled") is True
                    and recovery_budget_exhausted(board, task_id, phase)):
                runtime = runtime_escalation_entry(task_id, board)
                if runtime is None:
                    runtime = reserve_runtime_escalation(board, task_id, failed_run_id=failed["id"], phase=phase,
                                                         binding=binding, checkpoint=checkpoint)
                _reconcile_runtime_exhaustion_escalation(task_id, board, runtime)
                continue
            entry = reserve_recovery(board, task_id, failed_run_id=failed["id"], phase=phase, workspace_path=workspace, checkpoint=checkpoint, binding=binding, adopted=adopted)
            # Exact task evidence remains blocked after the durable reservation.
            if _show(task_id)["task"].get("status") != "blocked": continue
            value = _dispatch("kanban_unblock", {"task_id": task_id})
            if value.get("status") not in {"ready", "todo", "review"}: raise GateError("native unblock readback did not return a resumable status")
            update_recovery_identity(board, task_id, entry["failed_run_id"], entry["phase"], intent={"status": "unblock_verified", "native_status": value["status"]})
        except Exception:
            continue


def _routed_escalation_guard(task_id: str, board: str, entry: dict[str, Any]) -> dict[str, str] | None:
    """Admit only the exact live native worker on an already-published route.

    Reconciliation can be completed by the dispatch watchdog before a worker
    reaches its first tool.  Publication is not itself worker authority: each
    normal tool must still prove that this process owns the current native run
    in the route's implementation or review phase.
    """
    try:
        run_id, profile = _run_id(), worker_profile()
        binding = task_binding(task_id, board)
        if not binding or entry.get("binding") != binding:
            raise GateError("escalation binding is absent or changed")
        route = trusted_routing(task_id, board, binding)
        show = _show(task_id)
        task = show["task"]
        active = next((run for run in show["runs"] if run.get("id") == run_id), None)
        claimed = _event(show["events"], "claimed", run_id)
        payload = claimed.get("payload") if isinstance(claimed, dict) else None
        source = payload.get("source_status") if isinstance(payload, dict) else None
        # Native ready claims intentionally omit source_status.  That omission
        # is unambiguous only for the implementation side of an effective route.
        expected = (route["reviewer_profile"] if source == "review"
                    else route["implementation_profile"] if source in {None, "ready", "todo"}
                    else None)
        if (not isinstance(profile, str) or not profile or expected is None
                or task.get("status") != "running" or task.get("current_run_id") != run_id
                or task.get("assignee") != expected or profile != expected
                or not isinstance(active, dict) or active.get("profile") != expected
                or active.get("status") != "running" or active.get("ended_at") is not None
                or claimed is None):
            raise GateError("worker does not own the current effective escalation route")
    except Exception as exc:
        return {"action": "block", "message": f"Effective escalation route is not owned by this worker; refusing tool: {exc}"}
    return None


def guard(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, str] | None:
    """Protect only normal model tool calls; environment owns the worker task id."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if task_id:
        try:
            pending = escalation_entry(task_id, board_name())
            if pending and current_escalation_attempt(pending).get("intent", {}).get("status") == "changes_requested_pending":
                # Only the exact newly assigned target claim may turn a pending
                # intent into an active route. Old/wrong claims stay fenced.
                try:
                    run_id = _run_id()
                    profile = worker_profile()
                    status = _reconcile_pending_escalation(task_id, board_name(), required_run_id=run_id,
                                                           required_profile=profile)
                except Exception as exc:
                    return {"action": "block", "message": f"Escalation reconciliation is unreadable; refusing tool: {exc}"}
                if status != "routed":
                    held = "held" if status == "held" else "pending native reconciliation"
                    return {"action": "block", "message": f"Escalation routing is {held}; no worker tool is authorized. Stop work and wait for the exact routing readback or operator action."}
                # Do not return here: a just-reconciled route still has to pass
                # current-run admission and direct lifecycle calls still have
                # to reach their ordinary board-policy refusal below.
                pending = escalation_entry(task_id, board_name())
            if pending and current_escalation_attempt(pending).get("intent", {}).get("status") == "routed":
                admission = _routed_escalation_guard(task_id, board_name(), pending)
                if admission:
                    return admission
        except Exception as exc:
            return {"action": "block", "message": f"Escalation state is unreadable; refusing tool: {exc}"}
    recovery = _recovery_guard(tool_name)
    if recovery:
        return recovery
    if tool_name not in {"kanban_complete", "kanban_request_review", "kanban_request_changes"}:
        return None
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return None
    try:
        managed = is_managed(task_id, board_name())
    except Exception as exc:
        # model_tools intentionally isolates hook failures; turn every local
        # configuration/locking failure into a visible fail-closed directive.
        return {"action": "block", "message": f"Managed review configuration is unavailable; refusing {tool_name}: {exc}"}
    if not managed:
        return None
    attention = ""
    try:
        # Direct lifecycle calls on an activated board must first establish the
        # same immutable first-run binding as finish_implementation.  The call
        # is still refused below; this only records policy provenance before a
        # later supported handoff, never changes native Kanban.
        _binding(_show(task_id))
    except Exception as exc:
        attention = f" Board-policy task needs operator attention: {exc}"
    message = {
        "kanban_complete": "Managed task: use finish_implementation during implementation or submit_review for the reviewer verdict; direct completion is refused before Kanban mutates.",
        "kanban_request_review": "Managed task: use finish_implementation; it binds the configured reviewer and Git candidate.",
        "kanban_request_changes": "Managed review: use submit_review with a substantive rationale; it enforces reviewer identity and the change limit.",
    }[tool_name]
    return {"action": "block", "message": message + attention}


def _safe(handler):
    def call(args, **kwargs):
        try:
            return handler(args, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc:
            return json.dumps({"error": str(exc), "ok": False})
    return call


def register(ctx: Any) -> None:
    global _CONTEXT
    _CONTEXT = ctx
    ctx.register_tool(name="finish_implementation", toolset="local_first_review",
        schema={"name": "finish_implementation", "description": "Hand the current managed implementation to its configured reviewer session.",
                "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "artifacts": {"type": "array", "items": {"type": "string"}}}, "required": ["summary"], "additionalProperties": False}},
        handler=_safe(finish_implementation), description="Finish managed implementation through native review")
    ctx.register_tool(name="submit_review", toolset="local_first_review",
        schema={"name": "submit_review", "description": "Submit the configured reviewer's approval or required changes for the current managed task.",
                "parameters": {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["approved", "changes_requested"]}, "rationale": {"type": "string"}}, "required": ["verdict", "rationale"], "additionalProperties": False}},
        handler=_safe(submit_review), description="Submit a managed review verdict")
    ctx.register_hook("pre_tool_call", guard)
    ctx.register_hook("kanban_task_claimed", watchdog_claimed)
    ctx.register_hook("on_kanban_dispatch_tick", watchdog_tick)
