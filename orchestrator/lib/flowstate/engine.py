"""Flowstate engine: init, advance, retry, respawn, pause, resume, abort, status, events.

Correctness never depends on in-memory state. Every command re-reads state.yaml, every
transition is one atomic write under the state lock, and events are appended after the
change they describe. `advance`, `retry` and `respawn` hold a non-blocking lease so one
process drives a run at a time; `pause`, `abort` and `status` do not need it.

Node lifecycle (top level and inside branches alike):
    pending -> running -> completed
                       -> awaiting_decision (a persisted situation) -> retry/respawn -> running

Node execution lives in execution.py (scope-aware, non-blocking steps); fork, dynamic_fanout
and join live in parallel.py. This module drives the top-level cursor and handles decisions.
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agentctl.errors import AgentctlError

from . import agent_node, execution, outputs, parallel
from .errors import FlowstateError
from .loader import check_flow, load_flow
from .model import PARALLEL_KINDS, Flow, Node
from .paths import load_prefs, resolve_flow, resolve_run, runs_root
from .procs import detached_state, kill_detached
from .runtime import (RESPAWNABLE, RETRYABLE, feedback_message, load_run_flow, max_retries, public_vars,
                      run_level_situation, situation)
from .scope import TOP, Scope, new_node_state
from .state import RunStore, now_iso

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
POLL_S = 0.5
SCHEMA_VERSION = 2
RETRY_BUDGET_KINDS = {"agent", "script", "join"}


# ---------------------------------------------------------------- validate / init

def validate_flow(flow_ref: str | None) -> dict:
    dot, yml = resolve_flow(flow_ref, load_prefs())
    flow, issues = check_flow(dot, yml)
    return {"ok": not issues.errors, "flow": dot.stem, "dot": str(dot), "issues": issues,
            "inputs": sorted(flow.input_vars) if flow else [],
            "nodes": {n.id: n.kind for n in flow.nodes.values()} if flow else {},
            "regions": {r.parallel: {"kind": r.kind, "join": r.join, "nodes": sorted(r.nodes)}
                        for r in flow.regions.values()} if flow else {}}


def _coerce(decl, raw: str):
    try:
        if decl.type in ("string", "path", "any"):
            return raw
        if decl.type == "integer":
            return int(raw)
        if decl.type == "number":
            return float(raw) if any(c in raw for c in ".eE") else int(raw)
        if decl.type == "boolean":
            if raw.lower() in ("true", "false"):
                return raw.lower() == "true"
            raise ValueError(raw)
        value = json.loads(raw)
        if not isinstance(value, dict if decl.type == "dict" else list):
            raise ValueError(raw)
        return value
    except (ValueError, json.JSONDecodeError) as exc:
        raise FlowstateError("invalid_variable", f"--var {decl.name}: not a valid {decl.type}") from exc


def init_run(flow_ref: str | None, var_pairs: list[str] = (), *, run_id: str | None = None,
             runs_dir: str | None = None, harness: str = "claude", harness_opts: dict | None = None,
             fake_scripts: dict | None = None, supervision: str | None = None,
             max_retries_default: int | None = None) -> dict:
    prefs = load_prefs()
    dot, yml = resolve_flow(flow_ref, prefs)
    flow = load_flow(dot, yml)

    values = {}
    for pair in var_pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name:
            raise FlowstateError("invalid_variable", f"--var expects NAME=VALUE, got {pair!r}")
        if name not in flow.variables:
            raise FlowstateError("unknown_variable", f"{name!r} is not declared in {yml.name}")
        if name not in flow.input_vars:
            raise FlowstateError("not_an_input", f"{name!r} is produced by a node and cannot be set at init")
        values[name] = _coerce(flow.variables[name], raw)
    missing = []
    for name in sorted(flow.input_vars - set(values)):
        decl = flow.variables[name]
        if decl.has_default:
            values[name] = decl.default
        elif decl.required:
            missing.append(name)
    if missing:
        raise FlowstateError("missing_variable", f"required run variables not given: {missing}",
                             {"variables": missing})

    if harness not in ("claude", "fake"):
        raise FlowstateError("invalid_harness", f"harness must be claude or fake, got {harness!r}")
    scripts = {}
    for node_id, path in (fake_scripts or {}).items():
        if flow.nodes.get(node_id) is None or flow.nodes[node_id].kind != "agent":
            raise FlowstateError("invalid_fake_script", f"{node_id!r} is not an agent node")
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FlowstateError("invalid_fake_script", f"fake script not found: {resolved}")
        scripts[node_id] = str(resolved)
    for node in flow.nodes.values():
        if node.kind == "agent" and (node.harness or harness) == "fake" and node.id not in scripts:
            raise FlowstateError("fake_script_missing", f"agent node {node.id!r} needs --fake-script "
                                 f"{node.id}=PATH with the fake harness")
    supervision = supervision or prefs["supervision"]
    if supervision not in ("low", "medium", "high"):
        raise FlowstateError("invalid_supervision", "supervision must be low, medium or high")

    run_id = run_id or f"{flow.name}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    if not RUN_ID_RE.match(run_id):
        raise FlowstateError("invalid_run_id", f"invalid run id {run_id!r}")
    store = RunStore(runs_root(runs_dir) / run_id)
    store.create_layout()

    default_retries = int(prefs["max_retries"] if max_retries_default is None else max_retries_default)
    nodes = {}
    for n in flow.nodes.values():
        region = flow.region_of(n.id)
        budget = (n.max_retries if n.max_retries is not None else default_retries) \
            if n.kind in RETRY_BUDGET_KINDS else None
        # Region nodes execute per branch, under state.parallel; this entry only marks them.
        nodes[n.id] = {"kind": n.kind, "status": "in_branches", "parallel": region.parallel} if region \
            else new_node_state(n.kind, budget)
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "created_at": now_iso(),
        "flow": {"name": flow.name, "dot": str(dot), "yml": str(yml), "dir": str(flow.dir),
                 "digest": flow.digest},
        "config": {
            "harness": harness,
            "harness_opts": dict(harness_opts or {}),
            "fake_scripts": scripts,
            "supervision": supervision,
            "max_retries": default_retries,
            "stall_after_s": float(prefs["stall_after_s"]),
            "script_timeout_s": float(prefs["script_timeout_s"]),
            "max_parallel_branches": int(prefs["max_parallel_branches"]),
            "max_fanout_items": int(prefs["max_fanout_items"]),
        },
        "variables": {**values, "_run_id": run_id, "_run_dir": str(store.dir),
                      "_run_artefact_dir": str(store.artefacts), "_flow_dir": str(flow.dir)},
        "cursor": flow.start,
        "nodes": nodes,
        "parallel": {},
        "situation": None,
        "pause": None,
        "abort": None,
    }
    store.write_initial(state)
    store.event("run_created", run_id=run_id, flow=flow.name, digest=flow.digest, harness=harness,
                supervision=supervision, inputs=values)
    return {"run_id": run_id, "run_dir": str(store.dir), "flow": flow.name, "status": "running",
            "cursor": flow.start, "next": f"flowstate advance {run_id}"}


# ---------------------------------------------------------------- advance

def advance(run_ref: str, *, runs_dir: str | None = None, max_wait_s: float | None = None,
            stall_after_s: float | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    try:
        with store.advance_lease():
            return _advance(store, max_wait_s, stall_after_s)
    except FlowstateError as exc:
        if exc.code == "run_busy":
            return situation(store.read(), "busy", message=exc.message)
        raise


def _advance(store: RunStore, max_wait_s, stall_after_s) -> dict:
    state = store.read()
    early = run_level_situation(state)
    if early:
        return early
    try:
        flow = load_run_flow(state)
    except FlowstateError as exc:
        if exc.code in ("flow_changed", "invalid_flow"):
            return situation(state, "flow_changed", message=exc.message, details=exc.details)
        raise
    deadline = None if max_wait_s is None else time.monotonic() + max_wait_s

    while True:
        state = store.read()
        early = run_level_situation(state)
        if early:
            return early
        node = flow.nodes[state["cursor"]]
        if node.kind == "start":
            store.event("node_started", node=node.id, runner="start")
            step = execution.route_and_commit(store, flow, node, TOP, {})
            if step.kind == "decision":
                return step.situation
            continue
        if node.kind == "done":
            return _complete_run(store, node)
        if node.kind in PARALLEL_KINDS:
            result = parallel.drive(store, flow, node, deadline, stall_after_s)
            if result is not None:
                return result
            continue
        step = execution.step_node(store, flow, TOP, node.id, stall_after_s=stall_after_s)
        if step.kind in ("decision", "transient"):
            return step.situation
        if step.kind == "waiting":
            if deadline is not None and time.monotonic() >= deadline:
                kind = "worker_running" if node.kind == "agent" else "script_running"
                return situation(store.read(), kind, node.id, **(step.info or {}))
            time.sleep(POLL_S)


def _complete_run(store: RunStore, node: Node) -> dict:
    with store.transaction() as st:
        st["nodes"][node.id].update(status="completed", completed_at=now_iso())
        st["status"], st["cursor"], st["completed_at"] = "completed", None, now_iso()
    store.event("run_completed", node=node.id)
    final = store.read()
    return situation(final, "completed", variables=public_vars(final))


# ---------------------------------------------------------------- decisions

def _require_active(state: dict) -> None:
    if state["status"] in ("aborted", "completed"):
        raise FlowstateError("run_finished", f"run is {state['status']}")


def _require_node(flow: Flow, node_id: str) -> Node:
    if node_id not in flow.nodes:
        raise FlowstateError("unknown_node", f"no node {node_id!r} in flow {flow.name}")
    return flow.nodes[node_id]


def _resolve_scope(flow: Flow, state: dict, node_id: str, branch: str | None, wanted: set[str],
                   allow_running: bool = False) -> Scope:
    region = flow.region_of(node_id)
    if region is None:
        if branch:
            raise FlowstateError("not_in_branch", f"{node_id!r} is not inside a fork or dynamic_fanout")
        return TOP
    par = (state.get("parallel") or {}).get(region.parallel)
    if par is None:
        raise FlowstateError("no_branches", f"{region.parallel!r} has not created its branches yet")
    if branch:
        if branch not in par["branches"]:
            raise FlowstateError("unknown_branch", f"no branch {branch!r} in {region.parallel!r}",
                                 {"branches": par["branch_order"]})
        return Scope(region.parallel, branch)
    candidates = []
    for bid in par["branch_order"]:
        br = par["branches"][bid]
        sit = br.get("situation")
        if sit and sit.get("node") == node_id and sit["situation"] in wanted:
            candidates.append(bid)
        elif allow_running and not sit and br["cursor"] == node_id and br["nodes"][node_id]["status"] == "running":
            candidates.append(bid)
    if len(candidates) == 1:
        return Scope(region.parallel, candidates[0])
    raise FlowstateError("branch_required", f"{len(candidates)} branches of {region.parallel!r} match node "
                         f"{node_id!r}; pass --branch", {"branches": candidates})


def _exhausted(store: RunStore, node_id: str, previous: dict | None, limit: int, scope: Scope) -> dict:
    with store.transaction() as st:
        scope.node(st, node_id)["status"] = "awaiting_decision"
        sit = situation(st, "retries_exhausted", node_id, scope, max_retries=limit,
                        previous=(previous or {}).get("situation"), errors=(previous or {}).get("errors"))
        scope.set_situation(st, sit)
    store.event("retries_exhausted", node=node_id, max_retries=limit, previous=(previous or {}).get("situation"),
                **scope.labels())
    return sit


def retry(run_ref: str, node_id: str, feedback: str | None = None, runs_dir: str | None = None,
          branch: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.advance_lease():
        state = store.read()
        _require_active(state)
        flow = load_run_flow(state)
        node = _require_node(flow, node_id)
        scope = _resolve_scope(flow, state, node_id, branch, RETRYABLE)
        sit = scope.situation(state)
        if not sit or sit.get("node") != node_id or sit["situation"] not in RETRYABLE:
            raise FlowstateError("not_retryable", f"node {node_id!r} has no retryable situation",
                                 {"situation": (sit or {}).get("situation"), "retryable": sorted(RETRYABLE)})
        ns = scope.node(state, node_id)
        limit = max_retries(node, state)
        if ns["retries_used"] >= limit:
            return _exhausted(store, node_id, sit, limit, scope)
        used = ns["retries_used"] + 1
        result = {"ok": True, "action": "retry", "node": node_id, **scope.labels(), "retries_used": used,
                  "max_retries": limit, "next": f"flowstate advance {state['run_id']}"}

        if node.kind in ("script", "join"):
            with store.transaction() as st:
                n = scope.node(st, node_id)
                n.update(status="pending", retries_used=used)
                if node.kind == "script":
                    n["next_attempt_kind"] = "retry"
                scope.set_situation(st, None)
            store.event("retry_requested", node=node_id, retry_of=sit["situation"], feedback=feedback,
                        retries_used=used, max_retries=limit, **scope.labels())
            return result

        att = ns["attempts"][-1]
        if agent_node.worker_status(store, att["worker_id"])["state"] in ("running", "stalled"):
            raise FlowstateError("worker_busy", "the worker is still running; use respawn to replace it")
        paths = {k: Path(v) for k, v in ns.get("outputs", {}).items()}
        outputs.snapshot(paths, scope.log_dir(store.logs, node_id) / f"attempt-{att['index']}" / "before-retry")
        message = feedback_message(node_id, sit, att["session_id"], feedback, ns.get("outputs"))
        with store.transaction() as st:
            n = scope.node(st, node_id)
            n["attempts"].append({"index": len(n["attempts"]) + 1, "kind": "retry", "worker_id": att["worker_id"],
                                  "session_id": att["session_id"], "started_at": now_iso(), "launch": "pending",
                                  "retry_of": sit["situation"], "feedback": feedback, "message": message})
            n.update(status="running", retries_used=used)
            scope.set_situation(st, None)
        store.event("retry_requested", node=node_id, retry_of=sit["situation"], feedback=feedback,
                    worker_id=att["worker_id"], session_id=att["session_id"], retries_used=used,
                    max_retries=limit, **scope.labels())
        try:
            agent_node.send(store, att["worker_id"], message)
        except AgentctlError as exc:
            return execution.fail(store, node_id, "worker_failed", scope, worker_state="send_failed",
                                  message=str(exc)).situation
        execution.mark_launched(store, node_id, scope)
        return {**result, "worker_id": att["worker_id"], "session_id": att["session_id"]}


def respawn(run_ref: str, node_id: str, reason: str | None = None, runs_dir: str | None = None,
            branch: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.advance_lease():
        state = store.read()
        _require_active(state)
        flow = load_run_flow(state)
        node = _require_node(flow, node_id)
        if node.kind != "agent":
            raise FlowstateError("not_respawnable", "only agent nodes have workers to respawn")
        scope = _resolve_scope(flow, state, node_id, branch, RESPAWNABLE, allow_running=True)
        ns = scope.node(state, node_id)
        sit = scope.situation(state)
        if ns["status"] == "awaiting_decision":
            if not sit or sit.get("node") != node_id or sit["situation"] not in RESPAWNABLE:
                raise FlowstateError("not_respawnable", f"situation {(sit or {}).get('situation')!r} "
                                     "cannot be resolved by respawn")
        elif ns["status"] != "running" or not ns["attempts"]:
            raise FlowstateError("not_respawnable", f"node {node_id!r} is {ns['status']}")
        limit = max_retries(node, state)
        if ns["retries_used"] >= limit:
            return _exhausted(store, node_id, sit, limit, scope)

        old = ns["attempts"][-1]
        if agent_node.worker_status(store, old["worker_id"])["state"] in ("running", "stalled"):
            agent_node.kill(store, old["worker_id"])
            store.event("worker_killed", node=node_id, worker_id=old["worker_id"], reason="respawn", **scope.labels())
        paths = {k: Path(v) for k, v in ns.get("outputs", {}).items()}
        outputs.snapshot(paths, scope.log_dir(store.logs, node_id) / f"attempt-{old['index']}" / "before-respawn")
        count = sum(1 for a in ns["attempts"] if a["kind"] == "respawn") + 1
        new_worker, new_session = f"{scope.worker_id(node_id)}.respawn-{count}", str(uuid.uuid4())
        used = ns["retries_used"] + 1
        with store.transaction() as st:
            scope.node(st, node_id)["retries_used"] = used
        store.event("respawn_requested", node=node_id, reason=reason, old_worker_id=old["worker_id"],
                    old_session_id=old["session_id"], new_worker_id=new_worker, new_session_id=new_session,
                    retries_used=used, max_retries=limit, **scope.labels())
        step = execution.launch_agent(store, flow, node, scope, "respawn", session_id=new_session,
                                      worker_id=new_worker,
                                      replaces={"replaces_worker_id": old["worker_id"],
                                                "replaces_session_id": old["session_id"], "reason": reason})
        if step.kind == "decision":
            return step.situation
        return {"ok": True, "action": "respawn", "node": node_id, **scope.labels(),
                "old_worker_id": old["worker_id"], "old_session_id": old["session_id"], "worker_id": new_worker,
                "session_id": new_session, "retries_used": used, "max_retries": limit,
                "next": f"flowstate advance {state['run_id']}"}


def pause(run_ref: str, reason: str | None = None, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.transaction() as st:
        _require_active(st)
        if st["status"] == "paused":
            return {"ok": True, "status": "paused", "note": "already paused", "pause": st["pause"]}
        st["status"] = "paused"
        st["pause"] = {"reason": reason or "requested", "source": "command", "at": now_iso()}
    store.event("paused", reason=reason or "requested", source="command")
    return {"ok": True, "status": "paused",
            "note": "running workers are not stopped; advance will not start or finish nodes until resume"}


def resume(run_ref: str, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.transaction() as st:
        _require_active(st)
        if st["status"] != "paused":
            raise FlowstateError("not_paused", f"run is {st['status']}")
        info = st["pause"] or {}
        if info.get("node"):
            scope = Scope(info["parallel"], info["branch"]) if info.get("branch") else TOP
            scope.node(st, info["node"])["pause_acknowledged"] = True
        st["status"], st["pause"] = "running", None
    store.event("resumed", node=info.get("node"), branch=info.get("branch"))
    return {"ok": True, "status": "running", "next": f"flowstate advance {st['run_id']}"}


def _active_executions(state: dict) -> list[dict]:
    """Agent workers, detached scripts and reducers that may still be running."""
    found = []

    def visit(nodes: dict, labels: dict) -> None:
        for node_id, ns in nodes.items():
            if ns.get("status") not in ("running", "awaiting_decision") or not ns.get("attempts"):
                continue
            att = ns["attempts"][-1]
            if ns["kind"] == "agent":
                found.append({"kind": "agent", "node": node_id, "worker_id": att["worker_id"],
                              "session_id": att["session_id"], **labels})
            elif ns["kind"] == "script" and "exit_code" not in att:
                found.append({"kind": "script", "node": node_id, "log_dir": att["log_dir"], "stem": "script",
                              **labels})

    visit(state["nodes"], {})
    for pid, par in (state.get("parallel") or {}).items():
        for bid, br in par["branches"].items():
            visit(br["nodes"], {"parallel": pid, "branch": bid})
        runs = par["join_state"]["reducer_runs"]
        if runs and "outcome" not in runs[-1]:
            found.append({"kind": "reducer", "node": par["join"], "log_dir": runs[-1]["log_dir"],
                          "stem": parallel.REDUCER, "parallel": pid})
    return found


def abort(run_ref: str, reason: str, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.transaction() as st:
        _require_active(st)
        running = _active_executions(st)
        st["abort"] = {"reason": reason, "at": now_iso(), "last_situation": st["situation"]}
        st["status"], st["situation"] = "aborted", None
    store.event("aborted", reason=reason)
    killed_workers, killed_scripts = [], []
    for ex in running:
        labels = {k: ex[k] for k in ("parallel", "branch") if k in ex}
        if ex["kind"] == "agent":
            try:
                if agent_node.worker_status(store, ex["worker_id"])["state"] in ("running", "stalled"):
                    agent_node.kill(store, ex["worker_id"])
                    killed_workers.append(ex["worker_id"])
                    store.event("worker_killed", worker_id=ex["worker_id"], node=ex["node"], reason="abort", **labels)
            except AgentctlError:
                pass
        elif detached_state(Path(ex["log_dir"]), ex["stem"])["state"] == "running":
            kill_detached(Path(ex["log_dir"]), ex["stem"])
            killed_scripts.append(ex["log_dir"])
            store.event("script_killed", node=ex["node"], log_dir=ex["log_dir"], reason="abort", **labels)
    return {"ok": True, "status": "aborted", "reason": reason, "killed_workers": killed_workers,
            "killed_scripts": killed_scripts}


# ---------------------------------------------------------------- inspection

def status(run_ref: str, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    state = store.read()
    active = []
    for ex in _active_executions(state):
        labels = {k: ex[k] for k in ("parallel", "branch") if k in ex}
        if ex["kind"] == "agent":
            try:
                ws = agent_node.worker_status(store, ex["worker_id"])
            except AgentctlError:
                active.append({"node": ex["node"], "worker_id": ex["worker_id"], "state": "not_launched", **labels})
                continue
            if ws["state"] in ("running", "stalled"):
                active.append({"node": ex["node"], "worker_id": ex["worker_id"], "session_id": ex["session_id"],
                               "state": ws["state"], "idle_s": ws["idle_s"], **labels})
        elif detached_state(Path(ex["log_dir"]), ex["stem"])["state"] == "running":
            active.append({"node": ex["node"], "kind": ex["kind"], "state": "running", **labels})

    regions = {}
    for pid, par in (state.get("parallel") or {}).items():
        rows = []
        for bid in par["branch_order"]:
            br = par["branches"][bid]
            row = {"id": bid, "index": br["index"], "status": br["status"], "cursor": br["cursor"],
                   "workers": [a["worker_id"] for ns in br["nodes"].values() for a in ns["attempts"]
                               if a.get("worker_id")]}
            if "item" in br["context"]:
                row["item"] = br["context"]["item"]
            if br.get("situation"):
                row["situation"] = br["situation"]["situation"]
            rows.append(row)
        js = par["join_state"]
        regions[pid] = {"kind": par["kind"], "join": par["join"], "status": par["status"],
                        "counts": parallel._counts(par), "branches": rows,
                        "join_state": {"status": js["status"], "folded": len(js["folded"]),
                                       "merged_variables": sorted(js["merged_variables"] or {})}}

    cursor = state["cursor"]
    current = None
    if cursor:
        ns = state["nodes"][cursor]
        current = {"id": cursor, "kind": ns["kind"], "status": ns["status"], "attempts": len(ns.get("attempts", [])),
                   "retries_used": ns.get("retries_used", 0)}
    completed = sorted((n for n, ns in state["nodes"].items() if ns["status"] == "completed"),
                       key=lambda n: state["nodes"][n].get("completed_at", ""))
    return {"run_id": state["run_id"], "flow": state["flow"]["name"], "status": state["status"],
            "current_node": current, "completed_nodes": completed, "variables": state["variables"],
            "active_workers": active, "parallel": regions, "situation": state["situation"],
            "pause": state["pause"], "abort": state["abort"], "run_dir": str(store.dir),
            "revision": state["revision"], "updated_at": state["updated_at"]}


def events(run_ref: str, tail_n: int | None = None, kinds: list[str] | None = None,
           runs_dir: str | None = None) -> list[dict]:
    return RunStore(resolve_run(run_ref, runs_dir)).events(tail_n, set(kinds) if kinds else None)
