from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import subprocess
import json

from dashboard import plugin_api


@pytest.fixture(autouse=True)
def _present_profiles(monkeypatch):
    monkeypatch.setattr(plugin_api, "profile_exists", lambda name: True)


def client():
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def test_status_uses_native_snapshot_and_marks_counts_unknown(monkeypatch):
    monkeypatch.setattr(plugin_api, "load_state", lambda: {
        "version": 1, "implementation_profile": "impl", "reviewer_profile": "review",
        "tasks": {
            "default:one": {"board": "default", "task_id": "one", "implementation_profile": "impl",
                            "reviewer_profile": "review", "workspace_path": "/work"},
            "default:missing": {"board": "default", "task_id": "missing", "implementation_profile": "impl",
                                "reviewer_profile": "review", "workspace_path": "/work"},
        },
    })
    monkeypatch.setattr(plugin_api, "_observe_task", lambda board, task_id: (
        {"title": "Native task", "status": "done", "created_at": 1, "completed_at": 7},
        [
            {"id": 1, "profile": "impl", "status": "review", "outcome": "review_requested", "started_at": 2,
             "ended_at": 6, "metadata": {"local_first_review": {"implementation_run_id": 1,
                 "implementation_profile": "impl", "reviewer_profile": "review", "candidate": {"head": "a"}}}},
            {"id": 2, "profile": "review", "status": "done", "outcome": "completed", "summary": "approved summary",
             "started_at": 6, "ended_at": 7, "metadata": {"local_first_review": {"verdict": "approved",
                 "rationale": "checks pass", "implementation_run_id": 1, "reviewer_run_id": 2,
                 "reviewer_profile": "review", "candidate": {"head": "a"}}}},
        ],
        [],
    ) if task_id == "one" else (_ for _ in ()).throw(plugin_api.TelemetryUnavailable("native unavailable")))

    response = client().get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {name: None for name in plugin_api.COUNT_NAMES}
    task = body["tasks"][0]
    assert task["title"] == "Native task"
    assert task["phase"] == "approved_done"
    assert task["run_status"] == "ended"
    assert task["process_liveness"] == "unknown (not probed)"
    assert task["latest_verdict"] == "approved"
    assert task["changes_count"] == 0
    assert body["tasks"][1]["error"] == "native unavailable"


def test_done_without_approved_completion_metadata_stays_unknown(monkeypatch):
    monkeypatch.setattr(plugin_api, "_observe_task", lambda board, task_id: (
        {"title": "Native task", "status": "done", "created_at": 1, "completed_at": 7},
        [{"status": "done", "outcome": "completed", "summary": "completed", "started_at": 2, "ended_at": 7,
          "metadata": {"local_first_review": {"verdict": "changes_requested", "rationale": "not approved"}}}], [],
    ))

    task, observed = plugin_api._task_view({"board": "default", "task_id": "one", "implementation_profile": "impl",
                                             "reviewer_profile": "review", "workspace_path": "/work"})

    assert observed is True
    assert task["phase"] == "unknown"


def test_escalated_completion_uses_trusted_effective_route_not_original_binding(monkeypatch):
    binding = {"board": "default", "task_id": "one", "implementation_profile": "impl",
               "reviewer_profile": "review", "workspace_path": "/work"}
    route = {"board": "default", "task_id": "one", "implementation_profile": "strong",
             "reviewer_profile": "strong"}
    monkeypatch.setattr(plugin_api, "trusted_routing", lambda *_args, **_kwargs: route)
    candidate = {"head": "a"}
    runs = [
        {"id": 11, "profile": "strong", "status": "review", "outcome": "review_requested", "ended_at": 6,
         "metadata": {"local_first_review": {"implementation_run_id": 11, "implementation_profile": "strong",
                                                "reviewer_profile": "strong", "candidate": candidate}}},
        {"id": 12, "profile": "strong", "status": "done", "outcome": "completed", "ended_at": 7,
         "metadata": {"local_first_review": {"verdict": "approved", "reviewer_run_id": 12,
                                                "reviewer_profile": "strong", "implementation_run_id": 11,
                                                "candidate": candidate}}},
    ]

    assert plugin_api._completion_is_approved({"status": "done"}, runs, binding)
    runs[0]["metadata"]["local_first_review"]["implementation_profile"] = "impl"
    assert not plugin_api._completion_is_approved({"status": "done"}, runs, binding)


@pytest.mark.parametrize("enabled", [True, False])
def test_task_status_exposes_its_own_board_escalation_policy(monkeypatch, enabled):
    binding = {"board": "board-a", "task_id": "one", "implementation_profile": "impl",
               "reviewer_profile": "review", "workspace_path": "/work"}
    pending = {"current_attempt": 1, "attempts": [{"attempt": 1, "intent": {"status": "changes_requested_pending"}}]}
    monkeypatch.setattr(plugin_api, "effective_routing", lambda *_: None)
    monkeypatch.setattr(plugin_api, "trusted_routing", lambda *_: binding)
    monkeypatch.setattr(plugin_api, "escalation_entry", lambda *_: pending)
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"boards": {"board-a": {"escalation": {"enabled": enabled}}}})
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: ({"title": "Task", "status": "ready"}, [], []))

    task, _ = plugin_api._task_view(binding)

    assert task["escalation_status"] == "changes_requested_pending"
    assert task["escalation_enabled"] is enabled


def test_configuration_validates_and_updates_only_defaults(monkeypatch):
    stored = {"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "default:one": {"board": "default", "task_id": "one", "implementation_profile": "impl",
                        "reviewer_profile": "review", "workspace_path": "/work"}}}
    monkeypatch.setattr(plugin_api, "profiles", lambda: ["impl", "review", "new-impl", "new-review"])
    monkeypatch.setattr(plugin_api, "profile_exists", lambda name: True)
    monkeypatch.setattr(plugin_api, "locked_state", _memory_lock(stored))
    monkeypatch.setattr(plugin_api, "_view", lambda: {"configuration": dict(stored), "tasks": [], "counts": {}})

    response = client().put("/configuration", json={"implementation_profile": "new-impl", "reviewer_profile": "new-review"})

    assert response.status_code == 200
    assert stored["implementation_profile"] == "new-impl"
    assert stored["reviewer_profile"] == "new-review"
    assert stored["tasks"]["default:one"]["implementation_profile"] == "impl"
    same_profile = client().put("/configuration", json={"implementation_profile": "impl", "reviewer_profile": "impl"})
    assert same_profile.status_code == 200
    assert stored["implementation_profile"] == stored["reviewer_profile"] == "impl"
    assert client().put("/configuration", json={"implementation_profile": "impl", "reviewer_profile": "review", "extra": True}).status_code == 422


def test_configuration_rejects_deleted_profile_without_writing(monkeypatch):
    stored = {"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}}
    writes = []
    monkeypatch.setattr(plugin_api, "profiles", lambda: ["impl", "review"])
    monkeypatch.setattr(plugin_api, "profile_exists", lambda name: name == "impl")
    monkeypatch.setattr(plugin_api, "locked_state", lambda **kwargs: writes.append(kwargs))

    response = client().put("/configuration", json={"implementation_profile": "impl", "reviewer_profile": "review"})

    assert response.status_code == 409
    assert "missing" in response.json()["detail"]
    assert stored == {"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}}
    assert writes == []


def test_configuration_discovery_failure_is_safe_and_does_not_write(monkeypatch):
    writes = []
    monkeypatch.setattr(plugin_api, "profiles", lambda: (_ for _ in ()).throw(OSError("private path")))
    monkeypatch.setattr(plugin_api, "locked_state", lambda **kwargs: writes.append(kwargs))

    response = client().put("/configuration", json={"implementation_profile": "impl", "reviewer_profile": "review"})

    assert response.status_code == 503
    assert "profiles unavailable" in response.json()["detail"]
    assert "private path" not in response.json()["detail"]
    assert writes == []


def test_enroll_uses_board_task_observation_and_state_helper(monkeypatch):
    observed = []
    monkeypatch.setattr(plugin_api, "_observe_task", lambda board, task_id: observed.append((board, task_id)) or (
        {"id": task_id, "status": "blocked", "block_kind": "needs_input", "workspace_path": "/work"}, [], []))
    monkeypatch.setattr(plugin_api, "enroll_task", lambda *, board, task, runs: {"board": board, "task_id": task["id"]})
    monkeypatch.setattr(plugin_api, "_view", lambda: {"tasks": [], "counts": {}})

    response = client().post("/enroll", json={"board": "board-a", "task_id": "one"})

    assert response.status_code == 200
    assert observed == [("board-a", "one")]
    assert response.json()["enrollment"] == {"board": "board-a", "task_id": "one"}


def test_recovery_control_sets_explicit_limit_and_rejects_non_integer_pause_surrogates(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin_api, "set_recovery_policy", lambda board, *, enabled, max_per_phase=None:
                        calls.append((board, enabled, max_per_phase)) or {"recovery": {"enabled": enabled, "max_per_phase": max_per_phase}})

    response = client().put("/recovery", json={"board": "board-a", "enabled": True, "max_per_phase": 2})

    assert response.status_code == 200
    assert calls == [("board-a", True, 2)]
    for invalid in (0, True, 1.5, -1, plugin_api.RECOVERY_MAX_PER_PHASE_LIMIT + 1):
        response = client().put("/recovery", json={"board": "board-a", "enabled": True, "max_per_phase": invalid})
        assert response.status_code == 422
    assert calls == [("board-a", True, 2)]


@pytest.mark.parametrize("invalid", [0, 1, 1.0, "true", "false"], ids=["zero", "one", "float-one", "true-string", "false-string"])
def test_recovery_control_rejects_coerced_enabled_values_without_policy_mutation(monkeypatch, invalid):
    calls = []
    monkeypatch.setattr(plugin_api, "set_recovery_policy", lambda board, *, enabled, max_per_phase=None:
                        calls.append((board, enabled, max_per_phase)))

    response = client().put("/recovery", json={"board": "board-a", "enabled": invalid})

    assert response.status_code == 422
    assert calls == []


def test_shipped_dashboard_exposes_recovery_limit_not_zero_as_pause():
    bundle = (plugin_api.Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js").read_text()
    assert "max_per_phase" in bundle
    assert "Maximum failed-run recoveries per phase" in bundle
    assert "Use Pause failed-run recovery to stop automatic recovery." in bundle


def test_escalation_control_persists_future_only_routing_and_shipped_controls(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin_api, "set_escalation_policy", lambda board, **kwargs:
                        calls.append((board, kwargs)) or {"escalation": kwargs})

    response = client().put("/escalation", json={"board": "board-a", "enabled": True,
                                                   "normal_correction_limit": 1, "max_attempts": 1,
                                                   "implementation_profile": "strong", "reviewer_profile": "post-review"})

    assert response.status_code == 200
    assert calls == [("board-a", {"enabled": True, "normal_correction_limit": 1, "max_attempts": 1,
                                   "implementation_profile": "strong", "reviewer_profile": "post-review"})]
    assert "future exhaustion" in response.json()["message"]
    assert client().put("/escalation", json={"board": "board-a", "enabled": True,
                                               "normal_correction_limit": 0, "max_attempts": 1,
                                               "implementation_profile": "strong", "reviewer_profile": "post-review"}).status_code == 422
    bundle = (plugin_api.Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js").read_text()
    assert "Enable correction escalation" in bundle
    assert "held cards are never swept or rerouted" in bundle
    assert "targetImpl===targetReviewer" not in bundle


@pytest.mark.parametrize(("enabled", "expected"), [(True, "Reconciling"), (False, "Held (disabled)")])
def test_shipped_dashboard_renders_per_board_escalation_authority(enabled, expected):
    """Execute the shipped bundle: /status has no global escalation setting."""
    bundle = plugin_api.Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js"
    status = {"configuration": {"implementation_profile": "impl", "reviewer_profile": "review"},
              "board_policies": [{"board": "default", "implementation_profile": "impl", "reviewer_profile": "review",
                                 "escalation": {"enabled": False, "normal_correction_limit": 1, "max_attempts": 1,
                                                "implementation_profile": "strong", "reviewer_profile": "strong"}}],
              "counts": {}, "tasks": [{"board": "board-a", "task_id": "one", "phase": "changes_requested",
              "native_status": "ready", "run_status": "ended", "implementation_profile": "impl", "reviewer_profile": "review",
              "escalation_status": "changes_requested_pending", "escalation_enabled": enabled}]}
    script = f'''const fs=require("fs"),vm=require("vm");
let states=[], cursor=0, page; const h=(type,props,...children)=>({{type,props:props||{{}},children}});
const hooks={{useState:(initial)=>{{const i=cursor++; if(!(i in states)) states[i]=initial; return [states[i],v=>states[i]=v];}},useEffect:(fn)=>fn()}};
global.setInterval=()=>0; global.clearInterval=()=>{{}};
global.window={{__HERMES_PLUGIN_SDK__:{{React:{{createElement:h}},hooks,fetchJSON:(path)=>Promise.resolve(path.endsWith('/status')?{json.dumps(status)}:path.endsWith('/profiles')?{{profiles:[]}}:{{boards:[]}})}},__HERMES_PLUGINS__:{{register:(_,p)=>page=p}}}};
vm.runInThisContext(fs.readFileSync({str(bundle)!r},"utf8")); page(); Promise.resolve().then(()=>Promise.resolve()).then(()=>{{cursor=0; const before=page(); function nodes(v){{if(Array.isArray(v))return v.flatMap(nodes);if(v&&typeof v==="object")return [v,...nodes(v.children||[])];return []}} const input=nodes(before).find(x=>x.type==="input"&&x.props["aria-label"]==="Normal review corrections before escalation"); input.props.onChange({{target:{{value:""}}}}); cursor=0; const after=page(); console.log(JSON.stringify({{before,after}}));}});'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    rendered = json.loads(result.stdout)
    def nodes(value):
        if isinstance(value, dict):
            yield value
            for child in value.get("children", []):
                yield from nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from nodes(child)
    before, after = rendered["before"], rendered["after"]
    before_nodes, after_nodes = list(nodes(before)), list(nodes(after))
    numeric = next(node for node in after_nodes if node.get("type") == "input"
                   and node["props"].get("aria-label") == "Normal review corrections before escalation")
    assert numeric["props"]["value"] == ""
    for label in ("Save escalation policy", "Enable correction escalation"):
        matching = [node for node in before_nodes if node.get("type") == "button" and label in node.get("children", [])]
        assert len(matching) == 1
        assert matching[0]["props"]["disabled"] is False


@pytest.mark.parametrize(("fails", "expected"), [(True, "policy save refused"), (False, "Review-correction escalation policy saved.")])
def test_shipped_dashboard_surfaces_escalation_save_result_to_mobile_view(fails, expected):
    bundle = plugin_api.Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js"
    status = {"configuration": {"implementation_profile": "impl", "reviewer_profile": "review"},
              "board_policies": [{"board": "default", "implementation_profile": "impl", "reviewer_profile": "review",
                                  "escalation": {"enabled": False, "normal_correction_limit": 2, "max_attempts": 1,
                                                 "implementation_profile": "strong", "reviewer_profile": "strong"}}],
              "counts": {}, "tasks": []}
    mutation_response = 'Promise.reject(new Error("policy save refused"))' if fails else "Promise.resolve({})"
    script = f'''const fs=require("fs"),vm=require("vm");
let states=[],cursor=0,page,scrolls=[];const h=(type,props,...children)=>({{type,props:props||{{}},children}});
const hooks={{useState:(initial)=>{{const i=cursor++;if(!(i in states))states[i]=initial;return [states[i],v=>states[i]=v];}},useEffect:(fn)=>fn()}};
global.setInterval=()=>0;global.clearInterval=()=>{{}};
global.window={{scrollTo:(...args)=>scrolls.push(args),__HERMES_PLUGIN_SDK__:{{React:{{createElement:h}},hooks,fetchJSON:(path)=>path.endsWith('/status')?Promise.resolve({json.dumps(status)}):path.endsWith('/profiles')?Promise.resolve({{profiles:["strong"]}}):path.endsWith('/boards')?Promise.resolve({{boards:["default"]}}):path.endsWith('/escalation')?({mutation_response}):Promise.reject(new Error("unexpected route"))}},__HERMES_PLUGINS__:{{register:(_,p)=>page=p}}}};
(async()=>{{vm.runInThisContext(fs.readFileSync({str(bundle)!r},"utf8"));page();await Promise.resolve();await Promise.resolve();cursor=0;let tree=page();function nodes(v){{if(Array.isArray(v))return v.flatMap(nodes);if(v&&typeof v==="object")return [v,...nodes(v.children||[])];return []}}const save=nodes(tree).find(x=>x.type==="button"&&x.children.includes("Save escalation policy"));await save.props.onClick();await new Promise(r=>setTimeout(r,0));cursor=0;tree=page();const feedback=nodes(tree).filter(x=>x.props&&(x.props.role==="alert"||x.props.role==="status")).map(x=>x.children);console.log(JSON.stringify({{scrolls,feedback}}));}})();'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["scrolls"], rendered
    assert expected in str(rendered["feedback"])


def test_policy_scope_exposes_default_recovery_limit_for_legacy_policy(monkeypatch):
    policy = {"activation_id": "a", "native_run_watermark": 0,
              "implementation_profile": "impl", "reviewer_profile": "review"}
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"tasks": {}})
    monkeypatch.setattr(plugin_api, "_activation_preview", lambda *_args, **_kwargs: {
        "eligible_no_run": [], "awaiting_first_gate": [], "attention": [], "history": [], "legacy_bound": []})

    scope = plugin_api._policy_scope_view("board-a", policy)

    assert scope["recovery"] == {"enabled": False, "max_per_phase": 1}


def test_policy_preview_classifies_only_runtime_admissible_cards(monkeypatch):
    policy = {"activation_id": "a", "native_run_watermark": 4,
              "implementation_profile": "impl", "reviewer_profile": "review"}
    monkeypatch.setattr(plugin_api, "_observe_board", lambda board: [
        {"id": "eligible-no-run", "status": "todo", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "eligible-current", "status": "running", "current_run_id": 5, "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "wrong-assignee", "status": "todo", "assignee": "other", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "unassigned", "status": "todo", "assignee": None, "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "bad-workspace", "status": "todo", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "relative"},
        {"id": "shared-dir", "status": "todo", "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
        {"id": "prior-run", "status": "todo", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "preactivation", "status": "running", "current_run_id": 4, "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "blocked", "status": "blocked", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "done", "status": "done", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "legacy", "status": "todo", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
    ])
    monkeypatch.setattr(plugin_api, "_board_runs", lambda board, task_id: {
        "eligible-current": [{"id": 5, "profile": "impl", "status": "running", "ended_at": None}],
        "prior-run": [{"id": 3, "profile": "impl", "status": "ended", "ended_at": 9}],
        "preactivation": [{"id": 4, "profile": "impl", "status": "running", "ended_at": None}],
    }.get(task_id, []))

    preview = plugin_api._activation_preview("board-a", policy=policy, legacy_bound_ids={"legacy"})

    assert preview["eligible_no_run"] == ["eligible-no-run"]
    assert preview["awaiting_first_gate"] == ["eligible-current"]
    assert preview["history"] == ["done"]
    assert preview["legacy_bound"] == ["legacy"]
    assert {item["task_id"]: item["reason"] for item in preview["attention"]} == {
        "bad-workspace": "workspace is not an absolute task-scoped Git worktree",
        "shared-dir": "workspace is not an absolute task-scoped Git worktree",
        "blocked": "native status blocked is not eligible for dispatch",
        "preactivation": "current run predates this policy activation",
        "prior-run": "task already has native run history",
        "unassigned": "task is unassigned; assign the pinned implementation profile",
        "wrong-assignee": "task is assigned to another profile",
    }


def test_board_activation_publishes_policy_and_only_classifies_existing_history(monkeypatch):
    monkeypatch.setattr(plugin_api, "_observe_board", lambda board: [
        {"id": "future", "status": "ready", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"},
        {"id": "ran", "status": "ready", "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
        {"id": "done", "status": "done", "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
    ])
    monkeypatch.setattr(plugin_api, "_board_runs", lambda board, task_id: [] if task_id == "future" else [{"id": 9}])
    captured = []
    monkeypatch.setattr(plugin_api, "activate_board", lambda board, *, activation_id: captured.append((board, activation_id)) or {
        "activation_id": activation_id, "native_run_watermark": 9, "implementation_profile": "impl", "reviewer_profile": "review"})
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"tasks": {}})

    response = client().post("/board-activation", json={"board": "board-a"})

    assert response.status_code == 200
    body = response.json()
    assert body["preview"]["eligible_no_run"] == ["future"]
    assert body["preview"]["awaiting_first_gate"] == []
    assert body["preview"]["attention"][0]["task_id"] == "ran"
    assert body["preview"]["history"] == ["done"]
    assert captured and captured[0][0] == "board-a"


def test_status_shows_persistent_board_scope_and_current_classification_without_writing(monkeypatch):
    state = {"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {}, "boards": {
        "board-a": {"activation_id": "a", "native_run_watermark": 4,
                    "implementation_profile": "impl", "reviewer_profile": "review"}}}
    monkeypatch.setattr(plugin_api, "load_state", lambda: state)
    monkeypatch.setattr(plugin_api, "locked_state", lambda **kwargs: (_ for _ in ()).throw(AssertionError("GET wrote state")))
    monkeypatch.setattr(plugin_api, "_observe_board", lambda board: [
        {"id": "future", "status": "ready", "assignee": "impl", "workspace_kind": "worktree", "workspace_path": "/work"}, {"id": "old", "status": "running"},
        {"id": "completed", "status": "done"}, {"id": "reviewing", "status": "review"},
    ])
    monkeypatch.setattr(plugin_api, "_board_runs", lambda board, task_id: [] if task_id == "future" else [{"id": 1}])

    response = client().get("/status")

    assert response.status_code == 200
    scope = response.json()["board_policies"]
    assert scope[0]["board"] == "board-a"
    assert scope[0]["eligible_no_run"] == ["future"]
    assert scope[0]["awaiting_first_gate"] == []
    assert {item["task_id"] for item in scope[0]["attention"]} == {"old", "reviewing"}
    assert scope[0]["history"] == ["completed"]
    assert scope[0]["legacy_bound"] == []
    assert scope[0]["error"] is None


def test_legacy_binding_stays_in_native_counter_not_board_policy_twice(monkeypatch):
    state = {"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "board-a:legacy": {"board": "board-a", "task_id": "legacy", "implementation_profile": "impl",
                           "reviewer_profile": "review", "workspace_path": "/work"},
    }, "boards": {"board-a": {"activation_id": "a", "native_run_watermark": 4,
                                 "implementation_profile": "impl", "reviewer_profile": "review"}}}
    monkeypatch.setattr(plugin_api, "load_state", lambda: state)
    monkeypatch.setattr(plugin_api.time, "time", lambda: 3)
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: (
        {"title": "Legacy", "status": "running", "created_at": 1},
        [{"id": 5, "profile": "impl", "status": "running", "started_at": 2, "ended_at": None}], [],
    ))
    monkeypatch.setattr(plugin_api, "_observe_board", lambda board: [
        {"id": "legacy", "status": "running", "current_run_id": 5, "assignee": "impl",
         "workspace_kind": "dir", "workspace_path": "/work"},
    ])
    monkeypatch.setattr(plugin_api, "_board_runs", lambda *_: [{"id": 5, "profile": "impl", "status": "running", "ended_at": None}])

    body = client().get("/status").json()

    assert body["counts"]["implementation_active"] == 1
    scope = body["board_policies"][0]
    assert scope["legacy_bound"] == ["legacy"]
    assert scope["eligible_no_run"] == scope["awaiting_first_gate"] == scope["attention"] == []


def test_status_hides_archived_bound_and_unbound_cards_but_keeps_done_cards(monkeypatch):
    state = {"version": 3, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "board-a:archived-status": {"board": "board-a", "task_id": "archived-status", "implementation_profile": "impl",
                                      "reviewer_profile": "review", "workspace_path": "/work"},
        "board-a:archived-at": {"board": "board-a", "task_id": "archived-at", "implementation_profile": "impl",
                                  "reviewer_profile": "review", "workspace_path": "/work"},
        "board-a:done-unarchived": {"board": "board-a", "task_id": "done-unarchived", "implementation_profile": "impl",
                                      "reviewer_profile": "review", "workspace_path": "/work"},
    }, "boards": {"board-a": {"activation_id": "a", "native_run_watermark": 0,
                                 "implementation_profile": "impl", "reviewer_profile": "review"}}}
    monkeypatch.setattr(plugin_api, "load_state", lambda: state)
    monkeypatch.setattr(plugin_api.time, "time", lambda: 100)
    monkeypatch.setattr(plugin_api, "locked_state", lambda **kwargs: (_ for _ in ()).throw(AssertionError("GET wrote state")))
    monkeypatch.setattr(plugin_api, "_observe_board", lambda board: [
        {"id": "archived-status", "status": "archived", "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
        {"id": "archived-at", "status": "running", "archived_at": 99, "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
        {"id": "archived-unbound-status", "status": "archived", "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
        {"id": "archived-unbound-at", "status": "todo", "archived_at": 99, "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
        {"id": "done-unarchived", "status": "done", "assignee": "impl", "workspace_kind": "dir", "workspace_path": "/work"},
    ])
    monkeypatch.setattr(plugin_api, "_board_runs", lambda *_: [])
    monkeypatch.setattr(plugin_api, "_observe_task", lambda board, task_id: {
        "archived-status": ({"id": task_id, "title": "Archived by status", "status": "archived", "created_at": 1},
                            [{"id": 1, "status": "running", "started_at": 99, "ended_at": None}], []),
        "archived-at": ({"id": task_id, "title": "Archived by timestamp", "status": "running", "archived_at": 99, "created_at": 1},
                        [{"id": 2, "status": "running", "started_at": 99, "ended_at": None}], []),
        "done-unarchived": ({"id": task_id, "title": "Done", "status": "done", "created_at": 1}, [
            {"id": 3, "profile": "impl", "status": "review", "outcome": "review_requested", "ended_at": 98,
             "metadata": {"local_first_review": {"implementation_run_id": 3, "implementation_profile": "impl",
                          "reviewer_profile": "review", "candidate": {"head": "a"}}}},
            {"id": 4, "profile": "review", "status": "done", "outcome": "completed", "ended_at": 99,
             "metadata": {"local_first_review": {"verdict": "approved", "implementation_run_id": 3,
                          "reviewer_run_id": 4, "reviewer_profile": "review", "candidate": {"head": "a"}}}},
        ], []),
    }[task_id])

    body = client().get("/status").json()

    assert [task["task_id"] for task in body["tasks"]] == ["done-unarchived"]
    assert body["counts"] == {"implementation_active": 0, "awaiting_or_under_review": 0,
                              "changes_requested": 0, "approved_done": 1, "blocked_failed": 0}
    scope = body["board_policies"][0]
    assert scope["history"] == ["done-unarchived"]
    assert scope["eligible_no_run"] == scope["awaiting_first_gate"] == scope["attention"] == scope["legacy_bound"] == []
    assert state["tasks"].keys() == {"board-a:archived-status", "board-a:archived-at", "board-a:done-unarchived"}


def _memory_lock(data):
    from contextlib import contextmanager

    @contextmanager
    def lock(*, write=False):
        yield data
    return lock


def test_status_read_observation_is_current_and_get_never_locks_or_writes(monkeypatch):
    state = {"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "default:one": {"board": "default", "task_id": "one", "implementation_profile": "impl",
                        "reviewer_profile": "review", "workspace_path": "/work"}}}
    monkeypatch.setattr(plugin_api, "load_state", lambda: state)
    monkeypatch.setattr(plugin_api.time, "time", lambda: 1000)
    monkeypatch.setattr(plugin_api, "locked_state", lambda **kwargs: (_ for _ in ()).throw(AssertionError("GET wrote state")))
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: (
        {"title": "Native", "status": "review", "created_at": 1},
        [{"id": 4, "status": "review", "outcome": "review_requested", "summary": "handoff", "started_at": 3,
          "ended_at": 7, "metadata": {"local_first_review": {"candidate": {"head": "a"}}}}], [],
    ))

    response = client().get("/status")

    assert response.status_code == 200
    task = response.json()["tasks"][0]
    assert task["last_observed"] == 1000
    assert task["last_native_activity"] == 7


def test_stale_active_run_has_actionable_error_and_unknown_counts(monkeypatch):
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "default:one": {"board": "default", "task_id": "one", "implementation_profile": "impl",
                        "reviewer_profile": "review", "workspace_path": "/work"}}})
    monkeypatch.setattr(plugin_api.time, "time", lambda: 10000)
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: (
        {"title": "Native", "status": "running", "current_run_id": 3, "last_heartbeat_at": 1},
        [{"id": 3, "status": "running", "started_at": 1, "last_heartbeat_at": 1, "ended_at": None}],
        [{"kind": "claimed", "run_id": 3, "created_at": 1, "payload": {"source_status": "ready"}}],
    ))

    response = client().get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {name: None for name in plugin_api.COUNT_NAMES}
    assert body["tasks"][0]["phase"] == "implementation"
    assert "stale" in body["tasks"][0]["error"].lower()
    assert "worker" in body["tasks"][0]["next_step"].lower()


def test_changes_requested_uses_native_reviewer_run_summary_and_reason(monkeypatch):
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: (
        {"title": "Native", "status": "ready"},
        [
            {"id": 1, "profile": "impl", "status": "review", "outcome": "review_requested", "summary": "implementation handoff",
             "ended_at": 4, "metadata": {"local_first_review": {"candidate": {"head": "a"}}}},
            {"id": 2, "profile": "review", "status": "ready", "outcome": "changes_requested", "summary": "add regression coverage",
             "ended_at": 6, "metadata": None},
        ], [],
    ))

    task, observed = plugin_api._task_view({"board": "default", "task_id": "one", "implementation_profile": "impl",
                                             "reviewer_profile": "review", "workspace_path": "/work"})

    assert observed is True
    assert task["phase"] == "changes_requested"
    assert task["latest_summary"] == "add regression coverage"
    assert task["latest_verdict"] == "changes_requested"
    assert task["latest_reason"] == "add regression coverage"


def test_done_requires_bound_reviewer_completion_and_matching_handoff_candidate(monkeypatch):
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: (
        {"title": "Native", "status": "done"},
        [
            {"id": 1, "profile": "impl", "status": "review", "outcome": "review_requested", "ended_at": 2,
             "metadata": {"local_first_review": {"implementation_run_id": 1, "implementation_profile": "impl",
                          "reviewer_profile": "review", "candidate": {"head": "candidate"}}}},
            {"id": 2, "profile": "review", "status": "done", "outcome": "completed", "ended_at": 3,
             "metadata": {"local_first_review": {"verdict": "approved", "implementation_run_id": 1,
                          "reviewer_run_id": 2, "reviewer_profile": "review", "candidate": {"head": "candidate"}}}},
        ], [],
    ))
    binding = {"board": "default", "task_id": "one", "implementation_profile": "impl", "reviewer_profile": "review", "workspace_path": "/work"}

    task, observed = plugin_api._task_view(binding)

    assert observed is True
    assert task["phase"] == "approved_done"


def test_missing_bound_profile_is_actionable_and_makes_counts_unknown(monkeypatch):
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"version": 1, "implementation_profile": "impl", "reviewer_profile": "review", "tasks": {
        "default:one": {"board": "default", "task_id": "one", "implementation_profile": "impl",
                        "reviewer_profile": "review", "workspace_path": "/work"}}})
    monkeypatch.setattr(plugin_api, "profile_exists", lambda name: name == "impl")
    monkeypatch.setattr(plugin_api, "_observe_task", lambda *_: ({"title": "Native", "status": "review"}, [], []))

    response = client().get("/status")

    assert response.status_code == 200
    assert response.json()["counts"] == {name: None for name in plugin_api.COUNT_NAMES}
    assert "missing or deleted" in response.json()["tasks"][0]["error"]
