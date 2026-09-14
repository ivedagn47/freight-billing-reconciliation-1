"""Executing one agent or script node inside a scope (the top level or one branch).

Every function is a single non-blocking step: start work, observe it once, or finish it.
The top-level loop (engine.py) and the branch driver (parallel.py) call these repeatedly,
so a node behaves identically wherever it runs. Each transition is one locked write of
state.yaml followed by its event.

A Step says what happened:
    progress   state changed; call again
    waiting    work is running (worker or detached script); nothing to do yet
    blocked    ready to launch but the branch concurrency limit is reached
    arrived    a branch reached its join
    transient  a situation that is not persisted (stalled, timeout, paused)
    decision   a persisted situation that needs retry/respawn/abort
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentctl.errors import AgentctlError

from . import agent_node, conditions, gates, outputs, script_node
from .errors import FlowstateError
from .model import Flow, Node
from .procs import detached_state, tail
from .runtime import event_fields, node_vars, run_level_situation, situation
from .scope import Scope
from .state import RunStore, now_iso


@dataclass
class Step:
    kind: str
    situation: dict | None = None
    info: dict | None = None


def fail(store: RunStore, node_id: str, kind: str, scope: Scope, event: str | None = None, **details) -> Step:
    with store.transaction() as st:
        ns = scope.node(st, node_id)
        ns["status"] = "awaiting_decision"
        if ns["attempts"]:
            ns["attempts"][-1].setdefault("outcome", kind)
            ns["attempts"][-1].setdefault("finished_at", now_iso())
        sit = situation(st, kind, node_id, scope, **details)
        scope.set_situation(st, sit)
    store.event(event or kind, **event_fields(sit))
    if scope.is_branch:
        store.event("branch_failed", parallel=scope.parallel, branch=scope.branch, node=node_id, situation=kind)
    return Step("decision", sit)


def step_node(store: RunStore, flow: Flow, scope: Scope, node_id: str, *, allow_launch: bool = True,
              stall_after_s: float | None = None) -> Step:
    state = store.read()
    node = flow.nodes[node_id]
    ns = scope.node(state, node_id)
    if ns["status"] == "pending":
        paused = maybe_pause_at(store, state, node, scope)
        if paused:
            return Step("transient", paused)
        if not allow_launch:
            return Step("blocked")
        return start_script(store, flow, node, scope) if node.kind == "script" else \
            launch_agent(store, flow, node, scope, "spawn")
    if ns["status"] == "running":
        return check_script(store, flow, node, scope) if node.kind == "script" else \
            check_agent(store, flow, node, scope, stall_after_s)
    if ns["status"] == "awaiting_decision":
        return Step("decision", scope.situation(state))
    raise FlowstateError("corrupt_state", f"node {node_id!r} has unexpected status {ns['status']!r}")


def maybe_pause_at(store: RunStore, state: dict, node: Node, scope: Scope) -> dict | None:
    supervision = state["config"]["supervision"]
    wanted = node.pause_at == "always" or (node.pause_at == "optional" and supervision == "high")
    if not wanted or scope.node(state, node.id).get("pause_acknowledged"):
        return None
    reason = f"pause_at={node.pause_at} (supervision={supervision})"
    with store.transaction() as st:
        st["status"] = "paused"
        st["pause"] = {"reason": reason, "node": node.id, "source": "pause_at", "at": now_iso(), **scope.labels()}
    store.event("paused", node=node.id, reason=reason, source="pause_at", **scope.labels())
    return run_level_situation(store.read())


# ---------------------------------------------------------------- script nodes

def start_script(store: RunStore, flow: Flow, node: Node, scope: Scope) -> Step:
    state = store.read()
    try:
        paths = outputs.resolve_paths(flow, node, node_vars(state, node.id, scope=scope), store.dir)
    except FlowstateError as exc:
        return fail(store, node.id, "render_failed", scope, event="node_failed", message=exc.message)
    with store.transaction() as st:
        ns = scope.node(st, node.id)
        index = len(ns["attempts"]) + 1
        log_dir = scope.log_dir(store.logs, node.id) / f"attempt-{index}"
        ns["attempts"].append({"index": index, "kind": ns.pop("next_attempt_kind", None) or "run",
                               "started_at": now_iso(), "log_dir": str(log_dir)})
        ns["status"] = "running"
        ns.setdefault("started_at", now_iso())
        ns["outputs"] = {k: str(v) for k, v in paths.items()}
    store.event("node_started", node=node.id, runner="script", attempt=index, **scope.labels())
    try:
        pid = script_node.launch(flow, node, store.read(), store, log_dir, scope)
    except FlowstateError as exc:
        return fail(store, node.id, "render_failed", scope, event="node_failed", message=exc.message)
    with store.transaction() as st:
        scope.node(st, node.id)["attempts"][-1]["runner_pid"] = pid
    return Step("progress")


def check_script(store: RunStore, flow: Flow, node: Node, scope: Scope) -> Step:
    att = scope.node(store.read(), node.id)["attempts"][-1]
    log_dir = Path(att["log_dir"])
    if "exit_code" not in att:
        res = detached_state(log_dir, script_node.STEM)
        if res["state"] == "running":
            return Step("waiting", info={"pid": res["pid"]})
        if res["state"] == "lost":
            return fail(store, node.id, "node_interrupted", scope, event="node_failed",
                        message="the script's runner stopped without recording a result; its outcome is unknown",
                        evidence={"log_dir": str(log_dir)})
        with store.transaction() as st:
            scope.node(st, node.id)["attempts"][-1].update(
                finished_at=now_iso(), exit_code=res["exit_code"], timed_out=res["timed_out"],
                duration_s=res["duration_s"])
        att = {**att, "exit_code": res["exit_code"], "timed_out": res["timed_out"]}
    stderr = log_dir / f"{script_node.STEM}.stderr.log"
    if att["exit_code"] != 0 or att.get("timed_out"):
        return fail(store, node.id, "script_failed", scope, event="node_failed", exit_code=att["exit_code"],
                    timed_out=att.get("timed_out"), stderr_tail=tail(stderr, 20),
                    evidence={"stdout": str(log_dir / f"{script_node.STEM}.stdout.log"), "stderr": str(stderr)})
    paths = {k: Path(v) for k, v in scope.node(store.read(), node.id).get("outputs", {}).items()}
    return finish(store, flow, node, scope, paths, session_id=None)


# ---------------------------------------------------------------- agent nodes

def launch_agent(store: RunStore, flow: Flow, node: Node, scope: Scope, kind: str, *,
                 session_id: str | None = None, worker_id: str | None = None,
                 replaces: dict | None = None) -> Step:
    state = store.read()
    session_id = session_id or str(uuid.uuid4())
    worker_id = worker_id or scope.worker_id(node.id)
    try:
        ctx = agent_node.context(flow, node, state, store, session_id, scope)
    except FlowstateError as exc:
        return fail(store, node.id, "render_failed", scope, event="node_failed", message=exc.message)
    with store.transaction() as st:
        ns = scope.node(st, node.id)
        ns["attempts"].append({"index": len(ns["attempts"]) + 1, "kind": kind, "worker_id": worker_id,
                               "session_id": session_id, "started_at": now_iso(), "launch": "pending",
                               **(replaces or {})})
        ns["status"] = "running"
        ns.setdefault("started_at", now_iso())
        ns["outputs"] = {k: str(v) for k, v in ctx["paths"].items()}
        scope.set_situation(st, None)
    if kind == "spawn":
        store.event("node_started", node=node.id, runner="agent", **scope.labels())
    return spawn_worker(store, flow, node, scope, worker_id, session_id, ctx)


def spawn_worker(store, flow, node, scope, worker_id, session_id, ctx) -> Step:
    state = store.read()
    try:
        agent_node.spawn(flow, node, state, store, worker_id, session_id, ctx)
    except (AgentctlError, FlowstateError) as exc:
        return fail(store, node.id, "worker_failed", scope, worker_state="launch_failed", message=str(exc))
    mark_launched(store, node.id, scope)
    store.event("worker_started", node=node.id, worker_id=worker_id, session_id=session_id,
                harness=node.harness or state["config"]["harness"], model=node.model,
                prompt=str(store.workers / worker_id / "prompt.md"), **scope.labels())
    return Step("progress")


def mark_launched(store: RunStore, node_id: str, scope: Scope) -> None:
    with store.transaction() as st:
        scope.node(st, node_id)["attempts"][-1]["launch"] = "launched"


def recover_launch(store: RunStore, flow: Flow, node: Node, scope: Scope, att: dict) -> Step:
    """A previous process recorded the attempt but may have died before agentctl acted."""
    reg = agent_node.registry(store)
    if att["kind"] in ("spawn", "respawn"):
        if reg.exists(att["worker_id"]):
            mark_launched(store, node.id, scope)
            return Step("progress")
        ctx = agent_node.context(flow, node, store.read(), store, att["session_id"], scope)
        return spawn_worker(store, flow, node, scope, att["worker_id"], att["session_id"], ctx)
    attempts = scope.node(store.read(), node.id)["attempts"]
    expected = sum(1 for a in attempts if a.get("worker_id") == att["worker_id"])
    if reg.invocation_count(att["worker_id"]) < expected:
        agent_node.send(store, att["worker_id"], att["message"])
    mark_launched(store, node.id, scope)
    return Step("progress")


def _elapsed_s(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def check_agent(store: RunStore, flow: Flow, node: Node, scope: Scope, stall_after_s: float | None) -> Step:
    att = scope.node(store.read(), node.id)["attempts"][-1]
    if att.get("launch") == "pending":
        try:
            return recover_launch(store, flow, node, scope, att)
        except (AgentctlError, FlowstateError) as exc:
            return fail(store, node.id, "worker_failed", scope, worker_state="launch_failed", message=str(exc))

    worker = att["worker_id"]
    ws = agent_node.worker_status(store, worker, stall_after_s)
    base = {"worker_id": worker, "session_id": att["session_id"], "evidence": {"worker_dir": ws["paths"]["dir"]}}
    if ws["state"] in ("exited", "failed", "killed", "lost"):
        with store.transaction() as st:
            scope.node(st, node.id)["attempts"][-1].update(
                worker_state=ws["state"], exit_code=ws["exit_code"], cost_usd=ws["cost_usd"],
                num_turns=ws["num_turns"])
        if ws["state"] != "exited":
            return fail(store, node.id, "worker_failed", scope, worker_state=ws["state"], exit_code=ws["exit_code"],
                        subtype=ws["last_result"]["subtype"],
                        result_text=(ws["last_result"]["text"] or "")[-500:] or None,
                        stderr_tail=tail(ws["paths"]["stderr"], 20) or None, **base)
        store.event("worker_completed", node=node.id, worker_id=worker, cost_usd=ws["cost_usd"],
                    num_turns=ws["num_turns"], **scope.labels())
        paths = {k: Path(v) for k, v in scope.node(store.read(), node.id)["outputs"].items()}
        return finish(store, flow, node, scope, paths, session_id=att["session_id"])
    if ws["state"] == "stalled":
        if not att.get("stall_reported"):
            with store.transaction() as st:
                scope.node(st, node.id)["attempts"][-1]["stall_reported"] = True
            store.event("worker_stalled", node=node.id, worker_id=worker, idle_s=ws["idle_s"], **scope.labels())
            if scope.is_branch:
                store.event("branch_stalled", parallel=scope.parallel, branch=scope.branch, node=node.id)
        return Step("transient", situation(store.read(), "worker_stalled", node.id, scope, idle_s=ws["idle_s"],
                                           stall_after_s=ws["stall_after_s"], **base))
    if node.timeout and _elapsed_s(att["started_at"]) > node.timeout:
        store.event("worker_timeout", node=node.id, worker_id=worker, timeout_s=node.timeout, **scope.labels())
        return Step("transient", situation(store.read(), "worker_timeout", node.id, scope,
                                           timeout_s=node.timeout, **base))
    return Step("waiting", info={"idle_s": ws["idle_s"], **base})


# ---------------------------------------------------------------- validation, routing, commit

def finish(store: RunStore, flow: Flow, node: Node, scope: Scope, paths: dict[str, Path],
           session_id: str | None) -> Step:
    errors, docs = outputs.validate(flow, node, paths, session_id)
    values = {}
    if not errors:
        values, errors = outputs.bind(flow, node, paths, docs)
    if errors:
        index = len(scope.node(store.read(), node.id)["attempts"])
        saved = outputs.snapshot(paths, scope.log_dir(store.logs, node.id) / f"attempt-{index}" / "rejected")
        return fail(store, node.id, "validation_failed", scope, errors=errors,
                    evidence={"rejected_outputs": saved})
    store.event("outputs_validated", node=node.id, files=sorted(paths), session_checked=session_id is not None,
                **scope.labels())
    return route_and_commit(store, flow, node, scope, values)


def route_and_commit(store: RunStore, flow: Flow, node: Node, scope: Scope, values: dict,
                     extra_commit=None) -> Step:
    """Pick the edge, run its gates, then commit in one write. `extra_commit(state)` lets a
    caller (join completion) change more state inside that same write."""
    state = store.read()
    variables = {**node_vars(state, node.id, scope=scope), **values}
    edges = flow.out_edges(node.id)
    chosen = []
    for edge in edges:
        if edge.condition_tree is None:
            chosen.append(edge)
            continue
        try:
            if conditions.evaluate(edge.condition_tree, variables, edge.condition):
                chosen.append(edge)
        except FlowstateError as exc:
            return fail(store, node.id, "condition_error", scope, event="node_failed", edge=edge.id,
                        condition=edge.condition, message=exc.message)
    if not chosen:
        return fail(store, node.id, "no_route", scope, event="node_failed",
                    conditions={e.id: e.condition for e in edges})
    if len(chosen) > 1:
        return fail(store, node.id, "ambiguous_route", scope, event="node_failed", edges=[e.id for e in chosen])
    edge = chosen[0]

    if edge.gates:
        with store.transaction() as st:
            ns = scope.node(st, node.id)
            ns["gate_evaluations"] = ns.get("gate_evaluations", 0) + 1
            label = f"eval-{ns['gate_evaluations']}"
        logs_root = store.logs / "branches" / scope.branch if scope.is_branch else store.logs
        result = gates.run(flow, edge, variables, store.dir, logs_root, label)
        for r in result["results"]:
            if r["passed"]:
                store.event("gate_passed", node=node.id, edge=edge.id, gate=r["gate"], **scope.labels())
        if not result["passed"]:
            last = result["results"][-1]
            return fail(store, node.id, "gate_failed", scope, edge=edge.id, gate=last["gate"],
                        exit_code=last["exit_code"], timed_out=last["timed_out"],
                        stderr_tail=last["stderr_tail"] or None,
                        evidence={"stdout": last["stdout"], "stderr": last["stderr"]})

    arrived = scope.is_branch and edge.target == flow.regions[scope.parallel].join
    with store.transaction() as st:
        ns = scope.node(st, node.id)
        scope.commit(st, values)
        ns.update(status="completed", completed_at=now_iso(), edge_taken=edge.id)
        if ns["attempts"]:
            ns["attempts"][-1].update(outcome="completed")
            ns["attempts"][-1].setdefault("finished_at", now_iso())
        scope.set_situation(st, None)
        if arrived:
            scope.branch_state(st).update(cursor=None, status="completed", completed_at=now_iso())
        else:
            scope.set_cursor(st, edge.target)
        if extra_commit:
            extra_commit(st)
    store.event("node_completed", node=node.id, edge=edge.id, variables_set=sorted(values) or None, **scope.labels())
    if arrived:
        store.event("branch_completed", parallel=scope.parallel, branch=scope.branch, join=edge.target)
        return Step("arrived")
    return Step("progress")
