from local_first_review.review_evidence import approved_completion, changes_requested_verdicts


BINDING = {
    "board": "default", "task_id": "task", "implementation_profile": "impl",
    "reviewer_profile": "review", "workspace_path": "/work",
}
CANDIDATE = {"head": "a" * 40, "clean_tracked": True}


def _handoff(run_id=10, *, candidate=CANDIDATE, session="implementer-session"):
    return {
        "id": run_id, "profile": "impl", "status": "review", "outcome": "review_requested",
        "ended_at": 10, "metadata": {"worker_session_id": session, "local_first_review": {
            "implementation_run_id": run_id, "implementation_profile": "impl", "reviewer_profile": "review",
            "candidate": candidate, "binding": BINDING,
        }},
    }


def _review(run_id=11, *, outcome="changes_requested", session="reviewer-session", candidate=CANDIDATE):
    metadata = None
    if outcome == "completed":
        metadata = {"worker_session_id": session, "local_first_review": {"verdict": "approved", "implementation_run_id": 10,
                                            "reviewer_run_id": run_id, "reviewer_profile": "review",
                                            "candidate": candidate}}
    return {"id": run_id, "profile": "review", "status": "done", "outcome": outcome,
            "ended_at": 11, "metadata": metadata}


def _events(review_id=11, *, completed=False):
    return [
        {"kind": "review_requested", "run_id": 10},
        {"kind": "claimed", "run_id": review_id, "payload": {"source_status": "review"}},
        {"kind": "completed" if completed else "changes_requested", "run_id": review_id},
    ]


def test_changes_requested_deduplicates_a_replayed_native_reviewer_run():
    runs = [_handoff(), _review(), _review()]
    events = _events()

    verdicts = changes_requested_verdicts(runs, events, BINDING)

    assert [verdict["reviewer_run_id"] for verdict in verdicts] == [11]


def test_retrying_same_candidate_in_a_new_review_run_cannot_inflate_corrections():
    from local_first_review.review_evidence import correction_count
    runs = [_handoff(), _review(), _handoff(12), _review(13)]
    events = _events() + [
        {"kind":"review_requested","run_id":12},
        {"kind":"claimed","run_id":13,"payload":{"source_status":"review"}},
        {"kind":"changes_requested","run_id":13},
    ]
    assert correction_count(runs,events,BINDING)==1
    assert len(changes_requested_verdicts(runs,events,BINDING))==2  # retain complete findings


def test_changes_requested_rejects_missing_native_event():
    runs = [_handoff(), _review()]

    assert changes_requested_verdicts(runs, _events()[:-1], BINDING) == []


def test_changes_requested_retains_distinct_candidates_from_one_reviewer_session():
    next_candidate = {"head": "b" * 40, "clean_tracked": True}
    runs = [_handoff(), _review(), _handoff(12, candidate=next_candidate), _review(13, session="reviewer-session")]
    events = _events() + [
        {"kind": "review_requested", "run_id": 12},
        {"kind": "claimed", "run_id": 13, "payload": {"source_status": "review"}},
        {"kind": "changes_requested", "run_id": 13},
    ]

    verdicts = changes_requested_verdicts(runs, events, BINDING)

    assert [verdict["reviewer_run_id"] for verdict in verdicts] == [11, 13]


def test_approval_requires_exact_handoff_binding_and_distinct_claimed_session():
    runs = [_handoff(), _review(outcome="completed")]

    assert approved_completion({"status": "done"}, runs, _events(completed=True), BINDING, BINDING)
    runs[0]["metadata"]["local_first_review"]["binding"] = {**BINDING, "workspace_path": "/other"}
    assert not approved_completion({"status": "done"}, runs, _events(completed=True), BINDING, BINDING)


def test_routed_runtime_negative_verdict_blocks_without_requesting_changes(monkeypatch):
    from local_first_review import plugin

    review = {"candidate": CANDIDATE, "implementation_run_id": 10}
    show = {"runs": [_handoff()], "events": _events()}
    calls = []
    monkeypatch.setattr(plugin, "_session", lambda: "reviewer-session")
    monkeypatch.setattr(plugin, "_review_context", lambda: ("task", BINDING, show, 11, "/work", review))
    monkeypatch.setattr(plugin, "runtime_escalation_entry", lambda *_: {"intent": {"status": "routed"}})
    monkeypatch.setattr(plugin, "_changes_count", lambda *_: (_ for _ in ()).throw(AssertionError("ordinary correction path used")))
    monkeypatch.setattr(plugin, "_dispatch", lambda tool, args: calls.append((tool, args)) or {"ok": True})
    monkeypatch.setattr(plugin, "_show", lambda *_: {"runs": [{"id": 11, "ended_at": 12}], "events": []})

    result = plugin.submit_review({"verdict": "changes_requested", "rationale": "The runtime replacement still misses the regression."})

    assert calls == [("kanban_block", {"reason": "Runtime escalation is exhausted; independent reviewer requested changes. Latest findings: The runtime replacement still misses the regression.", "kind": "needs_input"})]
    assert '"verdict": "changes_requested"' in result
