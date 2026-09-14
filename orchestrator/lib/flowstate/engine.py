"""Flowstate engine: init, advance, retry, respawn, pause, resume, abort, status, events.

Correctness never depends on in-memory state. Every command re-reads state.yaml, every
transition is one atomic write under the state lock, and events are appended after the
change they describe. `advance`, `retry` and `respawn` hold a non-blocking lease so one
process drives a run at a time; `pause`, `abort` and `status` do not need it.

Node lifecycle in state.yaml:
    pending -> running -> completed
                       -> awaiting_decision (a persisted situation) -> retry/respawn -> running

An agent attempt is recorded with launch: pending *before* agentctl is called and flipped to
launched afterwards, so a crashed advance reconnects to (or finishes launching) that worker
rather than spawning a second one. Completed nodes are never re-executed.
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agentctl.errors import AgentctlError

from . import agent_node, conditions, gates, outputs, script_node
from .errors import FlowstateError
from .loader import check_flow, load_flow
from .model import Flow, Node
from .paths import load_prefs, resolve_flow, resolve_run, runs_root
from .procs import tail
from .runtime import (RESPAWNABLE, RETRYABLE, feedback_message, load_run_flow, max_retries,
                      node_vars, situation)
from .state import RunStore, now_iso

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
POLL_S = 0.5
SCHEMA_VERSION = 1


# ---------------------------------------------------------------- validate / init

def validate_flow(flow_ref: str | None) -> dict:
    dot, yml = resolve_flow(flow_ref, load_prefs())
    flow, issues = check_flow(dot, yml)
    return {"ok": not issues.errors, "flow": dot.stem, "dot": str(dot), "issues": issues,
            "inputs": sorted(flow.input_vars) if flow else [],
            "nodes": {n.id: n.kind for n in flow.nodes.values()} if flow else {}}


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
            "max_retries": int(prefs["max_retries"] if max_retries_default is None else max_retries_default),
            "stall_after_s": float(prefs["stall_after_s"]),
            "script_timeout_s": float(prefs["script_timeout_s"]),
        },
        "variables": {**values, "_run_id": run_id, "_run_dir": str(store.dir),
                      "_run_artefact_dir": str(store.artefacts), "_flow_dir": str(flow.dir)},
        "cursor": flow.start,
        "nodes": {n.id: {"kind": n.kind, "status": "pending", "retries_used": 0, "attempts": []}
                  for n in flow.nodes.values()},
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
    early = _run_level_situation(state)
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
        early = _run_level_situation(state)
        if early:
            return early
        node = flow.nodes[state["cursor"]]
        ns = state["nodes"][node.id]

        if node.kind == "start":
            store.event("node_started", node=node.id, runner="start")
            result = _route_and_commit(store, flow, node, {})
        elif node.kind == "done":
            return _complete_run(store, node)
        elif ns["status"] == "pending":
            result = _maybe_pause_at(store, state, node)
            if result is None:
                result = _start_script(store, flow, node) if node.kind == "script" else \
                    _launch_agent(store, flow, node, "spawn")
        elif ns["status"] == "running":
            result = _resume_script(store, flow, node) if node.kind == "script" else \
                _poll_agent(store, flow, node, deadline, stall_after_s)
        else:
            raise FlowstateError("corrupt_state", f"cursor node {node.id!r} has status {ns['status']!r} "
                                 "but no pending situation")
        if result is not None:
            return result


def _run_level_situation(state: dict) -> dict | None:
    if state["status"] == "aborted":
        return situation(state, "aborted", reason=(state.get("abort") or {}).get("reason"))
    if state["status"] == "completed":
        return situation(state, "completed", variables=_public_vars(state))
    if state["status"] == "paused":
        pause = dict(state.get("pause") or {})
        pause.pop("at", None)
        return situation(state, "paused", pause.pop("node", None), paused_at=(state.get("pause") or {}).get("at"),
                         **pause)
    return state.get("situation")


def _public_vars(state: dict) -> dict:
    return {k: v for k, v in state["variables"].items() if not k.startswith("_")}


def _complete_run(store: RunStore, node: Node) -> dict:
    with store.transaction() as st:
        st["nodes"][node.id].update(status="completed", completed_at=now_iso())
        st["status"], st["cursor"], st["completed_at"] = "completed", None, now_iso()
    store.event("run_completed", node=node.id)
    return situation(store.read(), "completed", variables=_public_vars(store.read()))


def _maybe_pause_at(store: RunStore, state: dict, node: Node) -> dict | None:
    wanted = node.pause_at == "always" or (node.pause_at == "optional" and state["config"]["supervision"] == "high")
    if not wanted or state["nodes"][node.id].get("pause_acknowledged"):
        return None
    reason = f"pause_at={node.pause_at} (supervision={state['config']['supervision']})"
    with store.transaction() as st:
        st["status"] = "paused"
        st["pause"] = {"reason": reason, "node": node.id, "source": "pause_at", "at": now_iso()}
    store.event("paused", node=node.id, reason=reason, source="pause_at")
    return _run_level_situation(store.read())


def _fail(store: RunStore, node_id: str, kind: str, event: str | None = None, **details) -> dict:
    with store.transaction() as st:
        ns = st["nodes"][node_id]
        ns["status"] = "awaiting_decision"
        if ns["attempts"]:
            ns["attempts"][-1].setdefault("outcome", kind)
            ns["attempts"][-1].setdefault("finished_at", now_iso())
        sit = situation(st, kind, node_id, **details)
        st["situation"] = sit
    store.event(event or kind, **{k: v for k, v in sit.items() if k not in ("run_id", "options", "at")})
    return sit


# ---------------------------------------------------------------- script nodes

def _start_script(store: RunStore, flow: Flow, node: Node) -> dict | None:
    state = store.read()
    try:
        paths = outputs.resolve_paths(flow, node, node_vars(state, node.id), store.dir)
    except FlowstateError as exc:
        return _fail(store, node.id, "render_failed", event="node_failed", message=exc.message)
    with store.transaction() as st:
        ns = st["nodes"][node.id]
        index = len(ns["attempts"]) + 1
        ns["attempts"].append({"index": index, "kind": ns.pop("next_attempt_kind", None) or "run",
                               "started_at": now_iso(), "log_dir": str(store.logs / node.id / f"attempt-{index}")})
        ns["status"] = "running"
        ns.setdefault("started_at", now_iso())
        ns["outputs"] = {k: str(v) for k, v in paths.items()}
    store.event("node_started", node=node.id, runner="script", attempt=index)
    try:
        result, paths = script_node.run(flow, node, store.read(), store, index)
    except FlowstateError as exc:
        return _fail(store, node.id, "render_failed", event="node_failed", message=exc.message)
    with store.transaction() as st:
        st["nodes"][node.id]["attempts"][-1].update(
            finished_at=now_iso(), exit_code=result["exit_code"], timed_out=result["timed_out"],
            duration_s=result["duration_s"])
    return _after_script(store, flow, node, result["exit_code"], result["timed_out"],
                         {"stdout": result["stdout"], "stderr": result["stderr"]})


def _after_script(store, flow, node, exit_code, timed_out, evidence) -> dict | None:
    if exit_code != 0 or timed_out:
        return _fail(store, node.id, "script_failed", event="node_failed", exit_code=exit_code,
                     timed_out=timed_out, stderr_tail=tail(evidence["stderr"], 20), evidence=evidence)
    paths = {k: Path(v) for k, v in store.read()["nodes"][node.id].get("outputs", {}).items()}
    return _finish(store, flow, node, paths, session_id=None)


def _resume_script(store: RunStore, flow: Flow, node: Node) -> dict | None:
    att = store.read()["nodes"][node.id]["attempts"][-1]
    if "finished_at" not in att:
        return _fail(store, node.id, "node_interrupted", event="node_failed",
                     message="the script was running when a previous advance stopped; its result is unknown",
                     evidence={"log_dir": att["log_dir"]})
    log_dir = Path(att["log_dir"])
    return _after_script(store, flow, node, att.get("exit_code"), att.get("timed_out"),
                         {"stdout": str(log_dir / "script.stdout.log"), "stderr": str(log_dir / "script.stderr.log")})


# ---------------------------------------------------------------- agent nodes

def _launch_agent(store: RunStore, flow: Flow, node: Node, kind: str, *, session_id: str | None = None,
                  worker_id: str | None = None, replaces: dict | None = None) -> dict | None:
    state = store.read()
    session_id = session_id or str(uuid.uuid4())
    worker_id = worker_id or node.id
    try:
        ctx = agent_node.context(flow, node, state, store, session_id)
    except FlowstateError as exc:
        return _fail(store, node.id, "render_failed", event="node_failed", message=exc.message)
    with store.transaction() as st:
        ns = st["nodes"][node.id]
        ns["attempts"].append({"index": len(ns["attempts"]) + 1, "kind": kind, "worker_id": worker_id,
                               "session_id": session_id, "started_at": now_iso(), "launch": "pending",
                               **(replaces or {})})
        ns["status"] = "running"
        ns.setdefault("started_at", now_iso())
        ns["outputs"] = {k: str(v) for k, v in ctx["paths"].items()}
        st["situation"] = None
    if kind == "spawn":
        store.event("node_started", node=node.id, runner="agent")
    return _spawn_worker(store, flow, node, worker_id, session_id, ctx)


def _spawn_worker(store, flow, node, worker_id, session_id, ctx) -> dict | None:
    state = store.read()
    try:
        agent_node.spawn(flow, node, state, store, worker_id, session_id, ctx)
    except (AgentctlError, FlowstateError) as exc:
        return _fail(store, node.id, "worker_failed", worker_state="launch_failed", message=str(exc))
    _mark_launched(store, node.id)
    store.event("worker_started", node=node.id, worker_id=worker_id, session_id=session_id,
                harness=node.harness or state["config"]["harness"], model=node.model,
                prompt=str(store.workers / worker_id / "prompt.md"))
    return None


def _mark_launched(store: RunStore, node_id: str) -> None:
    with store.transaction() as st:
        st["nodes"][node_id]["attempts"][-1]["launch"] = "launched"


def _recover_launch(store: RunStore, flow: Flow, node: Node, att: dict) -> dict | None:
    """A previous process recorded the attempt but may have died before agentctl acted."""
    reg = agent_node.registry(store)
    if att["kind"] in ("spawn", "respawn"):
        if reg.exists(att["worker_id"]):
            _mark_launched(store, node.id)
            return None
        ctx = agent_node.context(flow, node, store.read(), store, att["session_id"])
        return _spawn_worker(store, flow, node, att["worker_id"], att["session_id"], ctx)
    expected = sum(1 for a in store.read()["nodes"][node.id]["attempts"] if a.get("worker_id") == att["worker_id"])
    if reg.invocation_count(att["worker_id"]) < expected:
        agent_node.send(store, att["worker_id"], att["message"])
    _mark_launched(store, node.id)
    return None


def _elapsed_s(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def _poll_agent(store: RunStore, flow: Flow, node: Node, deadline, stall_after_s) -> dict | None:
    att = store.read()["nodes"][node.id]["attempts"][-1]
    if att.get("launch") == "pending":
        try:
            result = _recover_launch(store, flow, node, att)
        except (AgentctlError, FlowstateError) as exc:
            return _fail(store, node.id, "worker_failed", worker_state="launch_failed", message=str(exc))
        if result is not None:
            return result
        att = store.read()["nodes"][node.id]["attempts"][-1]

    worker = att["worker_id"]
    while True:
        current = store.read()
        early = _run_level_situation(current)
        if early:
            return early
        ws = agent_node.worker_status(store, worker, stall_after_s)
        base = {"worker_id": worker, "session_id": att["session_id"],
                "evidence": {"worker_dir": ws["paths"]["dir"]}}
        if ws["state"] in ("exited", "failed", "killed", "lost"):
            with store.transaction() as st:
                st["nodes"][node.id]["attempts"][-1].update(
                    worker_state=ws["state"], exit_code=ws["exit_code"],
                    cost_usd=ws["cost_usd"], num_turns=ws["num_turns"])
            if ws["state"] != "exited":
                return _fail(store, node.id, "worker_failed", worker_state=ws["state"], exit_code=ws["exit_code"],
                             subtype=ws["last_result"]["subtype"],
                             result_text=(ws["last_result"]["text"] or "")[-500:] or None,
                             stderr_tail=tail(ws["paths"]["stderr"], 20) or None, **base)
            store.event("worker_completed", node=node.id, worker_id=worker, cost_usd=ws["cost_usd"],
                        num_turns=ws["num_turns"])
            paths = {k: Path(v) for k, v in store.read()["nodes"][node.id]["outputs"].items()}
            return _finish(store, flow, node, paths, session_id=att["session_id"])
        if ws["state"] == "stalled":
            if not att.get("stall_reported"):
                with store.transaction() as st:
                    st["nodes"][node.id]["attempts"][-1]["stall_reported"] = True
                store.event("worker_stalled", node=node.id, worker_id=worker, idle_s=ws["idle_s"])
            return situation(store.read(), "worker_stalled", node.id, idle_s=ws["idle_s"],
                             stall_after_s=ws["stall_after_s"], **base)
        if node.timeout and _elapsed_s(att["started_at"]) > node.timeout:
            store.event("worker_timeout", node=node.id, worker_id=worker, timeout_s=node.timeout)
            return situation(store.read(), "worker_timeout", node.id, timeout_s=node.timeout, **base)
        if deadline is not None and time.monotonic() >= deadline:
            return situation(store.read(), "worker_running", node.id, idle_s=ws["idle_s"], **base)
        time.sleep(POLL_S)


# ---------------------------------------------------------------- validation, routing, commit

def _finish(store: RunStore, flow: Flow, node: Node, paths: dict[str, Path], session_id: str | None) -> dict | None:
    errors, docs = outputs.validate(flow, node, paths, session_id)
    values = {}
    if not errors:
        values, errors = outputs.bind(flow, node, paths, docs)
    if errors:
        index = len(store.read()["nodes"][node.id]["attempts"])
        saved = outputs.snapshot(paths, store.logs / node.id / f"attempt-{index}" / "rejected")
        return _fail(store, node.id, "validation_failed", errors=errors, evidence={"rejected_outputs": saved})
    store.event("outputs_validated", node=node.id, files=sorted(paths), session_checked=session_id is not None)
    return _route_and_commit(store, flow, node, values)


def _route_and_commit(store: RunStore, flow: Flow, node: Node, values: dict) -> dict | None:
    state = store.read()
    scope = {**state["variables"], **values}
    edges = flow.out_edges(node.id)
    chosen = []
    for edge in edges:
        if edge.condition_tree is None:
            chosen.append(edge)
            continue
        try:
            if conditions.evaluate(edge.condition_tree, scope, edge.condition):
                chosen.append(edge)
        except FlowstateError as exc:
            return _fail(store, node.id, "condition_error", event="node_failed", edge=edge.id,
                         condition=edge.condition, message=exc.message)
    if not chosen:
        return _fail(store, node.id, "no_route", event="node_failed",
                     conditions={e.id: e.condition for e in edges})
    if len(chosen) > 1:
        return _fail(store, node.id, "ambiguous_route", event="node_failed", edges=[e.id for e in chosen])
    edge = chosen[0]

    if edge.gates:
        with store.transaction() as st:
            ns = st["nodes"][node.id]
            ns["gate_evaluations"] = ns.get("gate_evaluations", 0) + 1
            label = f"eval-{ns['gate_evaluations']}"
        result = gates.run(flow, edge, scope, store.dir, store.logs, label)
        for r in result["results"]:
            if r["passed"]:
                store.event("gate_passed", node=node.id, edge=edge.id, gate=r["gate"])
        if not result["passed"]:
            last = result["results"][-1]
            return _fail(store, node.id, "gate_failed", edge=edge.id, gate=last["gate"],
                         exit_code=last["exit_code"], timed_out=last["timed_out"],
                         stderr_tail=last["stderr_tail"] or None,
                         evidence={"stdout": last["stdout"], "stderr": last["stderr"]})

    with store.transaction() as st:
        ns = st["nodes"][node.id]
        st["variables"].update(values)
        ns.update(status="completed", completed_at=now_iso(), edge_taken=edge.id)
        if ns["attempts"]:
            ns["attempts"][-1].update(outcome="completed")
            ns["attempts"][-1].setdefault("finished_at", now_iso())
        st["cursor"] = edge.target
        st["situation"] = None
    store.event("node_completed", node=node.id, edge=edge.id, variables_set=sorted(values) or None)
    return None


# ---------------------------------------------------------------- decisions

def _require_active(state: dict) -> None:
    if state["status"] in ("aborted", "completed"):
        raise FlowstateError("run_finished", f"run is {state['status']}")


def _require_node(flow: Flow, node_id: str) -> Node:
    if node_id not in flow.nodes:
        raise FlowstateError("unknown_node", f"no node {node_id!r} in flow {flow.name}")
    return flow.nodes[node_id]


def _exhausted(store: RunStore, node_id: str, previous: dict | None, limit: int) -> dict:
    with store.transaction() as st:
        st["nodes"][node_id]["status"] = "awaiting_decision"
        sit = situation(st, "retries_exhausted", node_id, max_retries=limit,
                        previous=(previous or {}).get("situation"), errors=(previous or {}).get("errors"))
        st["situation"] = sit
    store.event("retries_exhausted", node=node_id, max_retries=limit, previous=(previous or {}).get("situation"))
    return sit


def retry(run_ref: str, node_id: str, feedback: str | None = None, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.advance_lease():
        state = store.read()
        _require_active(state)
        flow = load_run_flow(state)
        node = _require_node(flow, node_id)
        sit = state.get("situation")
        if not sit or sit.get("node") != node_id or sit["situation"] not in RETRYABLE:
            raise FlowstateError("not_retryable", f"node {node_id!r} has no retryable situation",
                                 {"situation": (sit or {}).get("situation"), "retryable": sorted(RETRYABLE)})
        ns = state["nodes"][node_id]
        limit = max_retries(node, state)
        if ns["retries_used"] >= limit:
            return _exhausted(store, node_id, sit, limit)

        if node.kind == "script":
            with store.transaction() as st:
                n = st["nodes"][node_id]
                n.update(status="pending", next_attempt_kind="retry", retries_used=n["retries_used"] + 1)
                st["situation"] = None
            store.event("retry_requested", node=node_id, retry_of=sit["situation"], feedback=feedback,
                        retries_used=ns["retries_used"] + 1, max_retries=limit)
            return {"ok": True, "action": "retry", "node": node_id, "retries_used": ns["retries_used"] + 1,
                    "max_retries": limit, "next": f"flowstate advance {state['run_id']}"}

        att = ns["attempts"][-1]
        if agent_node.worker_status(store, att["worker_id"])["state"] in ("running", "stalled"):
            raise FlowstateError("worker_busy", "the worker is still running; use respawn to replace it")
        paths = {k: Path(v) for k, v in ns.get("outputs", {}).items()}
        outputs.snapshot(paths, store.logs / node_id / f"attempt-{att['index']}" / "before-retry")
        message = feedback_message(node_id, sit, att["session_id"], feedback, ns.get("outputs"))
        with store.transaction() as st:
            n = st["nodes"][node_id]
            n["attempts"].append({"index": len(n["attempts"]) + 1, "kind": "retry", "worker_id": att["worker_id"],
                                  "session_id": att["session_id"], "started_at": now_iso(), "launch": "pending",
                                  "retry_of": sit["situation"], "feedback": feedback, "message": message})
            n.update(status="running", retries_used=n["retries_used"] + 1)
            st["situation"] = None
        store.event("retry_requested", node=node_id, retry_of=sit["situation"], feedback=feedback,
                    worker_id=att["worker_id"], session_id=att["session_id"],
                    retries_used=ns["retries_used"] + 1, max_retries=limit)
        try:
            agent_node.send(store, att["worker_id"], message)
        except AgentctlError as exc:
            return _fail(store, node_id, "worker_failed", worker_state="send_failed", message=str(exc))
        _mark_launched(store, node_id)
        return {"ok": True, "action": "retry", "node": node_id, "worker_id": att["worker_id"],
                "session_id": att["session_id"], "retries_used": ns["retries_used"] + 1, "max_retries": limit,
                "next": f"flowstate advance {state['run_id']}"}


def respawn(run_ref: str, node_id: str, reason: str | None = None, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.advance_lease():
        state = store.read()
        _require_active(state)
        flow = load_run_flow(state)
        node = _require_node(flow, node_id)
        if node.kind != "agent":
            raise FlowstateError("not_respawnable", "only agent nodes have workers to respawn")
        ns = state["nodes"][node_id]
        sit = state.get("situation")
        if ns["status"] == "awaiting_decision":
            if not sit or sit.get("node") != node_id or sit["situation"] not in RESPAWNABLE:
                raise FlowstateError("not_respawnable", f"situation {(sit or {}).get('situation')!r} "
                                     "cannot be resolved by respawn")
        elif ns["status"] != "running" or not ns["attempts"]:
            raise FlowstateError("not_respawnable", f"node {node_id!r} is {ns['status']}")
        limit = max_retries(node, state)
        if ns["retries_used"] >= limit:
            return _exhausted(store, node_id, sit, limit)

        old = ns["attempts"][-1]
        if agent_node.worker_status(store, old["worker_id"])["state"] in ("running", "stalled"):
            agent_node.kill(store, old["worker_id"])
            store.event("worker_killed", node=node_id, worker_id=old["worker_id"], reason="respawn")
        paths = {k: Path(v) for k, v in ns.get("outputs", {}).items()}
        outputs.snapshot(paths, store.logs / node_id / f"attempt-{old['index']}" / "before-respawn")
        count = sum(1 for a in ns["attempts"] if a["kind"] == "respawn") + 1
        new_worker, new_session = f"{node_id}.respawn-{count}", str(uuid.uuid4())
        with store.transaction() as st:
            st["nodes"][node_id]["retries_used"] += 1
        store.event("respawn_requested", node=node_id, reason=reason, old_worker_id=old["worker_id"],
                    old_session_id=old["session_id"], new_worker_id=new_worker, new_session_id=new_session,
                    retries_used=ns["retries_used"] + 1, max_retries=limit)
        result = _launch_agent(store, flow, node, "respawn", session_id=new_session, worker_id=new_worker,
                               replaces={"replaces_worker_id": old["worker_id"],
                                         "replaces_session_id": old["session_id"], "reason": reason})
        if result is not None:
            return result
        return {"ok": True, "action": "respawn", "node": node_id, "old_worker_id": old["worker_id"],
                "old_session_id": old["session_id"], "worker_id": new_worker, "session_id": new_session,
                "retries_used": ns["retries_used"] + 1, "max_retries": limit,
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
        pause_info = st["pause"] or {}
        if pause_info.get("node"):
            st["nodes"][pause_info["node"]]["pause_acknowledged"] = True
        st["status"], st["pause"] = "running", None
    store.event("resumed", node=pause_info.get("node"))
    return {"ok": True, "status": "running", "next": f"flowstate advance {st['run_id']}"}


def abort(run_ref: str, reason: str, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    with store.transaction() as st:
        _require_active(st)
        workers = [ns["attempts"][-1]["worker_id"] for ns in st["nodes"].values()
                   if ns["kind"] == "agent" and ns["status"] in ("running", "awaiting_decision") and ns["attempts"]]
        st["abort"] = {"reason": reason, "at": now_iso(), "last_situation": st["situation"]}
        st["status"], st["situation"] = "aborted", None
    store.event("aborted", reason=reason)
    killed = []
    for worker in workers:
        try:
            if agent_node.worker_status(store, worker)["state"] in ("running", "stalled"):
                agent_node.kill(store, worker)
                killed.append(worker)
                store.event("worker_killed", worker_id=worker, reason="abort")
        except AgentctlError:
            pass
    return {"ok": True, "status": "aborted", "reason": reason, "killed_workers": killed}


# ---------------------------------------------------------------- inspection

def status(run_ref: str, runs_dir: str | None = None) -> dict:
    store = RunStore(resolve_run(run_ref, runs_dir))
    state = store.read()
    active = []
    for node_id, ns in state["nodes"].items():
        if ns["kind"] == "agent" and ns["status"] == "running" and ns["attempts"]:
            att = ns["attempts"][-1]
            try:
                ws = agent_node.worker_status(store, att["worker_id"])
                active.append({"node": node_id, "worker_id": att["worker_id"], "session_id": att["session_id"],
                               "state": ws["state"], "idle_s": ws["idle_s"]})
            except AgentctlError:
                active.append({"node": node_id, "worker_id": att["worker_id"], "state": "not_launched"})
    cursor = state["cursor"]
    current = None
    if cursor:
        ns = state["nodes"][cursor]
        current = {"id": cursor, "kind": ns["kind"], "status": ns["status"], "attempts": len(ns["attempts"]),
                   "retries_used": ns["retries_used"]}
    completed = sorted((n for n, ns in state["nodes"].items() if ns["status"] == "completed"),
                       key=lambda n: state["nodes"][n].get("completed_at", ""))
    return {"run_id": state["run_id"], "flow": state["flow"]["name"], "status": state["status"],
            "current_node": current, "completed_nodes": completed, "variables": state["variables"],
            "active_workers": active, "situation": state["situation"], "pause": state["pause"],
            "abort": state["abort"], "run_dir": str(store.dir), "revision": state["revision"],
            "updated_at": state["updated_at"]}


def events(run_ref: str, tail_n: int | None = None, kinds: list[str] | None = None,
           runs_dir: str | None = None) -> list[dict]:
    return RunStore(resolve_run(run_ref, runs_dir)).events(tail_n, set(kinds) if kinds else None)
