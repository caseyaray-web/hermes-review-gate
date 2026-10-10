"""Validate native review verdict provenance without inferring it from outcomes."""
from __future__ import annotations

import json
from typing import Any


_BINDING_FIELDS = (
    "board", "task_id", "implementation_profile", "reviewer_profile", "workspace_path",
    "policy_activation_id", "policy_native_run_watermark", "native_run_id",
)


def _event(events: list[dict[str, Any]], kind: str, run_id: int) -> bool:
    return any(isinstance(event, dict) and event.get("kind") == kind and event.get("run_id") == run_id
                 for event in events)


def _claimed_from_review(events: list[dict[str, Any]], run_id: int) -> bool:
    return any(isinstance(event, dict) and event.get("kind") == "claimed" and event.get("run_id") == run_id
               and isinstance(event.get("payload"), dict) and event["payload"].get("source_status") == "review"
               for event in events)


def _packet(run: dict[str, Any]) -> dict[str, Any] | None:
    metadata = run.get("metadata")
    packet = metadata.get("local_first_review") if isinstance(metadata, dict) else None
    return packet if isinstance(packet, dict) else None


def _session(run: dict[str, Any]) -> str | None:
    metadata = run.get("metadata")
    value = metadata.get("worker_session_id") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value else None


def _expected_binding(binding: dict[str, Any]) -> dict[str, Any]:
    return {field: binding[field] for field in _BINDING_FIELDS if field in binding}


def _reviewer_identity(run: dict[str, Any]) -> tuple[str, int] | None:
    run_id, profile = run.get("id"), run.get("profile")
    return (profile, run_id) if isinstance(profile, str) and type(run_id) is int and run_id > 0 else None


def _candidate_key(candidate: Any) -> str | None:
    if not isinstance(candidate, dict) or candidate.get("clean_tracked") is not True:
        return None
    head = candidate.get("head")
    if not isinstance(head, str) or len(head) != 40 or any(char not in "0123456789abcdef" for char in head.lower()):
        return None
    try:
        return json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        return None


def _handoff_for(run: dict[str, Any], runs: list[dict[str, Any]], events: list[dict[str, Any]],
                 binding: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the exact immutable packet that authorized this reviewer run."""
    review_run_id = run.get("id")
    if type(review_run_id) is not int or review_run_id <= 0 or not _claimed_from_review(events, review_run_id):
        return None
    handoff_ids = [
        event.get("run_id") for event in events
        if isinstance(event, dict) and event.get("kind") == "review_requested"
        and type(event.get("run_id")) is int and event["run_id"] < review_run_id
    ]
    if not handoff_ids:
        return None
    handoff_id = handoff_ids[-1]
    handoff = next((candidate for candidate in runs if isinstance(candidate, dict) and candidate.get("id") == handoff_id), None)
    packet = _packet(handoff) if isinstance(handoff, dict) else None
    if (not isinstance(handoff, dict) or handoff.get("outcome") != "review_requested"
            or handoff.get("status") != "review" or handoff.get("ended_at") is None or packet is None
            or packet.get("implementation_run_id") != handoff_id):
        return None
    if (handoff.get("profile") != packet.get("implementation_profile")
            or packet.get("binding") != _expected_binding(binding)
            or not isinstance(packet.get("implementation_profile"), str)
            or not isinstance(packet.get("reviewer_profile"), str)
            or _candidate_key(packet.get("candidate")) is None):
        return None
    if _session(handoff) is None:
        return None
    return handoff, packet


def changes_requested_verdicts(runs: list[dict[str, Any]], events: list[dict[str, Any]],
                                binding: dict[str, Any]) -> list[dict[str, Any]]:
    """Return unique, native-backed reviewer changes verdicts in run order."""
    valid: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, int]]] = set()
    for run in runs:
        if not isinstance(run, dict) or run.get("outcome") != "changes_requested" or run.get("ended_at") is None:
            continue
        reviewer_run_id = run.get("id")
        handoff = _handoff_for(run, runs, events, binding)
        reviewer = _reviewer_identity(run)
        if (type(reviewer_run_id) is not int or not _event(events, "changes_requested", reviewer_run_id)
                or handoff is None or reviewer is None):
            continue
        _implementation, packet = handoff
        if run.get("profile") != packet.get("reviewer_profile"):
            continue
        candidate_key = _candidate_key(packet["candidate"])
        assert candidate_key is not None
        identity = (candidate_key, reviewer)
        if identity in seen:
            continue
        seen.add(identity)
        valid.append({"reviewer_run_id": reviewer_run_id, "reviewer_profile": reviewer[0],
                      "reviewer_identity": {"profile": reviewer[0], "run_id": reviewer[1]},
                      "candidate": packet["candidate"], "summary": run.get("summary")})
    return valid


def correction_count(runs, events, binding):
    """A repeated review of an unchanged candidate is not another correction."""
    return len({_candidate_key(v['candidate']) for v in changes_requested_verdicts(runs, events, binding)})


def approved_completion(task: dict[str, Any], runs: list[dict[str, Any]], events: list[dict[str, Any]],
                         binding: dict[str, Any], route: dict[str, Any]) -> bool:
    """Accept a completed card only when its approval has exact immutable provenance."""
    if task.get("status") != "done":
        return False
    for run in reversed(runs):
        if not isinstance(run, dict) or run.get("outcome") != "completed" or run.get("ended_at") is None:
            continue
        review = _packet(run)
        reviewer_run_id = run.get("id")
        if (review is None or review.get("verdict") != "approved" or type(reviewer_run_id) is not int
                or review.get("reviewer_run_id") != reviewer_run_id
                or review.get("reviewer_profile") != run.get("profile")
                or not _event(events, "completed", reviewer_run_id)):
            return False
        handoff = _handoff_for(run, runs, events, binding)
        if handoff is None:
            return False
        implementation, packet = handoff
        if (_session(run) is None or _session(run) == _session(implementation)
                or review.get("candidate") != packet.get("candidate")
                or packet.get("implementation_profile") != route.get("implementation_profile")
                or packet.get("reviewer_profile") != route.get("reviewer_profile")
                or run.get("profile") != route.get("reviewer_profile")):
            return False
        return True
    return False
