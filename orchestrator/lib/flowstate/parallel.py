"""fork, dynamic_fanout and join.

While the top-level cursor is on a fork/dynamic_fanout node, `drive` owns the region:

1. Create branches once, in one write. Fork: one per outgoing edge, ids `<fork>-<entry>` in
   sorted entry order. Fan-out: one per list item, ids `<fanout>-0000`, `-0001`, ... by
   position. Each branch persists its item, a context (`_branch_id`, `_branch_index`,
   `_parallel_node`, `item`, and `_run_artefact_dir` = artefacts/branches/<id>) and a full
   node-state record per region node. An empty list creates no branches.
2. Step every branch without blocking (execution.step_node), starting at most
   `max_parallel` branches at a time. Agent workers (tmux) and scripts (detached runner)
   execute concurrently; this single process only observes and records.
3. Fold arrivals into the join's summary with the reducer, strictly in branch order: branch k
   is folded once it and every earlier branch have arrived.
4. When every branch has arrived and every fold is done, merge: fork branch variables by
   name; fan-out region variables become lists in branch order (`[]` when empty); the
   summary_var gets the folded summary (`{}` if nothing was folded). Then the join routes
   like any node, and completing the join, the parallel record and the cursor move is one write.

State lives under state.parallel.<node>; see CLAUDE.md for the full shape.
"""

import json
import time
from collections import Counter
from pathlib import Path

from . import execution
from .model import Flow, Node
from .procs import detached_state, flow_env, launch_detached, script_argv, tail
from .runtime import run_level_situation, situation
from .scope import TOP, Scope, new_node_state
from .state import RunStore, now_iso

POLL_S = 0.5
REDUCER = "reducer"
DEFAULT_MAX_PARALLEL = 4
DEFAULT_MAX_ITEMS = 500


def drive(store: RunStore, flow: Flow, node: Node, deadline: float | None,
          stall_after_s: float | None) -> dict | None:
    """Returns a situation, or None once the join has completed and the cursor has moved on."""
    if node.id not in store.read().get("parallel", {}):
        sit = _create(store, flow, node)
        if sit:
            return sit
    while True:
        state = store.read()
        early = run_level_situation(state)
        if early:
            return early
        par = state["parallel"][node.id]
        if par["status"] == "merged":
            return _complete_join(store, flow, node)

        live, transients, paused = _pass(store, flow, node, par, stall_after_s)
        if paused:
            return paused
        fold = _fold(store, flow, node)
        if isinstance(fold, dict):
            return fold

        state = store.read()
        par = state["parallel"][node.id]
        branches = [par["branches"][b] for b in par["branch_order"]]
        if all(b["status"] == "completed" for b in branches) and _folds_done(flow, par):
            _merge(store, flow, node)
            continue
        if fold == "progress":
            continue  # a fold just finished; the next one may be ready immediately
        failures = [b["situation"] for b in branches if b["status"] == "awaiting_decision" and b.get("situation")]
        timed_out = deadline is not None and time.monotonic() >= deadline
        if (live == 0 and fold is None) or timed_out:
            counts = _counts(par)
            store.event("join_waiting", parallel=node.id, join=par["join"], **counts)
            if failures:
                return {**failures[0], "branches": counts, "other_branch_situations": len(failures) - 1}
            if transients:
                return {**transients[0], "branches": counts}
            return situation(state, "branches_running", parallel_node=node.id, join=par["join"], branches=counts)
        time.sleep(POLL_S)


def _counts(par: dict) -> dict:
    counts = Counter(b["status"] for b in par["branches"].values())
    return {"total": len(par["branch_order"]), **{k: counts.get(k, 0) for k in
                                                  ("pending", "running", "awaiting_decision", "completed")}}


# ---------------------------------------------------------------- branch creation

def _items(store: RunStore, flow: Flow, node: Node, state: dict):
    raw = state["variables"].get(node.items)
    vtype = flow.variables[node.items].type
    if vtype == "path" or (vtype == "any" and isinstance(raw, str)):
        try:
            raw = json.loads(Path(raw).read_text())
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            return None, f"items variable {node.items!r} must point to a JSON list file: {exc}"
    if not isinstance(raw, list):
        return None, f"items variable {node.items!r} must hold a JSON list, got {type(raw).__name__}"
    limit = node.max_items or int(state["config"].get("max_fanout_items", DEFAULT_MAX_ITEMS))
    if len(raw) > limit:
        return None, f"{len(raw)} items exceeds max_items={limit}"
    return raw, None


def _create(store: RunStore, flow: Flow, node: Node) -> dict | None:
    state = store.read()
    region = flow.regions[node.id]
    items = None
    if node.kind == "fork":
        specs = [(f"{node.id}-{entry}", entry, None) for entry in sorted(region.branches)]
    else:
        items, error = _items(store, flow, node, state)
        if error:
            return execution.fail(store, node.id, "fanout_invalid", TOP, event="node_failed", message=error).situation
        width = max(4, len(str(max(len(items) - 1, 0))))
        entry = next(iter(region.branches))
        specs = [(f"{node.id}-{i:0{width}d}", entry, item) for i, item in enumerate(items)]
    max_parallel = node.max_parallel or int(state["config"].get("max_parallel_branches", DEFAULT_MAX_PARALLEL))

    with store.transaction() as st:
        if node.id in st.setdefault("parallel", {}):
            return None
        branches = {}
        for index, (bid, entry, item) in enumerate(specs):
            context = {"_branch_id": bid, "_branch_index": index, "_parallel_node": node.id,
                       "_run_artefact_dir": str(store.artefacts / "branches" / bid)}
            if node.kind == "dynamic_fanout":
                context["item"] = item
            branches[bid] = {"index": index, "entry": entry, "status": "pending", "cursor": entry,
                             "context": context, "variables": {}, "situation": None, "created_at": now_iso(),
                             "nodes": {n: new_node_state(flow.nodes[n].kind) for n in sorted(region.branches[entry])}}
        st["parallel"][node.id] = {
            "kind": node.kind, "join": region.join, "status": "running", "created_at": now_iso(),
            "items_var": node.items, "items": items, "max_parallel": max_parallel,
            "branch_order": [s[0] for s in specs], "branches": branches,
            "join_state": {"status": "waiting", "folded": [], "summary": {}, "reducer_runs": [],
                           "merged_variables": None},
        }
        st["nodes"][node.id].update(status="running", started_at=now_iso())
    for bid, _, _ in specs:
        (store.artefacts / "branches" / bid).mkdir(parents=True, exist_ok=True)
    store.event("node_started", node=node.id, runner=node.kind)
    if node.kind == "dynamic_fanout":
        store.event("fanout_empty" if not specs else "fanout_created", parallel=node.id, items_var=node.items,
                    count=len(specs), max_parallel=max_parallel)
    for index, (bid, entry, item) in enumerate(specs):
        store.event("branch_created", parallel=node.id, branch=bid, index=index, entry=entry,
                    item=item if node.kind == "dynamic_fanout" else None)
    return None


# ---------------------------------------------------------------- stepping branches

def _pass(store: RunStore, flow: Flow, node: Node, par: dict, stall_after_s: float | None):
    active = sum(1 for b in par["branches"].values() if b["status"] == "running")
    live, transients = 0, []
    for bid in par["branch_order"]:
        br = par["branches"][bid]
        if br["status"] in ("completed", "awaiting_decision"):
            continue
        if br["status"] == "pending":
            if active >= par["max_parallel"]:
                live += 1
                continue
            with store.transaction() as st:
                current = st["parallel"][node.id]["branches"][bid]
                if current["status"] == "pending":
                    current.update(status="running", started_at=now_iso())
            store.event("branch_started", parallel=node.id, branch=bid, index=br["index"])
            active += 1

        scope = Scope(node.id, bid)
        step = None
        while True:  # drain instant steps (launch -> check, finish -> next node)
            current = scope.branch_state(store.read())
            if current["status"] != "running":
                break
            step = execution.step_node(store, flow, scope, current["cursor"], stall_after_s=stall_after_s)
            if step.kind != "progress":
                break
        if step is None:
            continue
        if step.kind == "transient":
            if step.situation["situation"] == "paused":
                return live, transients, step.situation
            transients.append(step.situation)
        elif step.kind in ("waiting", "blocked"):
            live += 1
    return live, transients, None


# ---------------------------------------------------------------- reducer folding

def _folds_done(flow: Flow, par: dict) -> bool:
    join = flow.nodes[par["join"]]
    return not join.reducer_script or len(par["join_state"]["folded"]) == len(par["branch_order"])


def _fold(store: RunStore, flow: Flow, node: Node):
    """One non-blocking reducer step: None (nothing to do), "waiting", "progress", or a situation."""
    state = store.read()
    par = state["parallel"][node.id]
    js = par["join_state"]
    join = flow.nodes[par["join"]]
    if not join.reducer_script or len(js["folded"]) == len(par["branch_order"]):
        return None
    fold_index = len(js["folded"])
    bid = par["branch_order"][fold_index]
    if par["branches"][bid]["status"] != "completed":
        return None  # folding is strictly in branch order

    runs = js["reducer_runs"]
    current = runs[-1] if runs and runs[-1]["branch_id"] == bid and "outcome" not in runs[-1] else None
    if current is None:
        return _start_reducer(store, flow, node, join, state, bid, fold_index)

    log_dir = Path(current["log_dir"])
    res = detached_state(log_dir, REDUCER)
    if res["state"] == "running":
        return "waiting"
    evidence = {"log_dir": str(log_dir)}
    if res["state"] == "lost":
        return _reducer_fail(store, node, join, "reducer_interrupted", bid, fold_index, evidence=evidence,
                             message="the reducer's runner stopped without recording a result")
    stderr = log_dir / f"{REDUCER}.stderr.log"
    if res["exit_code"] != 0 or res["timed_out"]:
        return _reducer_fail(store, node, join, "reducer_failed", bid, fold_index, exit_code=res["exit_code"],
                             timed_out=res["timed_out"], stderr_tail=tail(stderr, 20) or None,
                             evidence={**evidence, "stderr": str(stderr)})
    try:
        summary = json.loads((log_dir / "summary-out.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _reducer_fail(store, node, join, "reducer_output_invalid", bid, fold_index, evidence=evidence,
                             message=f"summary-out.json is missing or not JSON: {exc}")
    if not isinstance(summary, dict):
        return _reducer_fail(store, node, join, "reducer_output_invalid", bid, fold_index, evidence=evidence,
                             message=f"{join.summary_var} must be a JSON object, got {type(summary).__name__}")
    with store.transaction() as st:
        j = st["parallel"][node.id]["join_state"]
        j["summary"] = summary
        j["folded"].append(bid)
        j["reducer_runs"][-1].update(outcome="completed", finished_at=now_iso(), exit_code=res["exit_code"],
                                     duration_s=res["duration_s"])
        if st["nodes"][join.id]["status"] == "running":
            st["nodes"][join.id]["status"] = "pending"
    store.event("reducer_completed", join=join.id, parallel=node.id, branch=bid, fold=fold_index,
                summary_keys=sorted(summary))
    return "progress"


def _start_reducer(store, flow, node, join, state, bid, fold_index):
    js = state["parallel"][node.id]["join_state"]
    run_index = len(js["reducer_runs"]) + 1
    log_dir = store.logs / "joins" / join.id / f"fold-{fold_index:04d}-{bid}-run{run_index}"
    log_dir.mkdir(parents=True, exist_ok=True)
    scope = Scope(node.id, bid)
    branch = scope.branch_state(state)
    (log_dir / "summary-in.json").write_text(json.dumps(js["summary"], indent=2) + "\n")
    (log_dir / "branch-variables.json").write_text(json.dumps(branch["variables"], indent=2) + "\n")
    variables = {**scope.variables(state), "_node_id": join.id}
    env = flow_env(variables, {
        "FLOWSTATE_RUN_ID": state["run_id"], "FLOWSTATE_RUN_DIR": str(store.dir), "FLOWSTATE_NODE": join.id,
        "FLOWSTATE_JOIN_NODE": join.id, "FLOWSTATE_PARALLEL_NODE": node.id, "FLOWSTATE_BRANCH_ID": bid,
        "FLOWSTATE_BRANCH_INDEX": str(branch["index"]),
        "FLOWSTATE_REDUCER_SUMMARY_IN": str(log_dir / "summary-in.json"),
        "FLOWSTATE_REDUCER_SUMMARY_OUT": str(log_dir / "summary-out.json"),
        "FLOWSTATE_REDUCER_BRANCH_VARS": str(log_dir / "branch-variables.json"),
    })
    with store.transaction() as st:
        st["parallel"][node.id]["join_state"]["reducer_runs"].append(
            {"run": run_index, "branch_id": bid, "fold": fold_index, "log_dir": str(log_dir),
             "started_at": now_iso()})
        st["nodes"][join.id]["status"] = "running"
    store.event("reducer_started", join=join.id, parallel=node.id, branch=bid, fold=fold_index, run=run_index)
    pid = launch_detached(script_argv(join.reducer_script), store.artefacts, env,
                          state["config"]["script_timeout_s"], log_dir, REDUCER)
    with store.transaction() as st:
        st["parallel"][node.id]["join_state"]["reducer_runs"][-1]["runner_pid"] = pid
    return "waiting"


def _reducer_fail(store, node, join, kind, bid, fold_index, **details) -> dict:
    with store.transaction() as st:
        st["parallel"][node.id]["join_state"]["reducer_runs"][-1].update(outcome=kind, finished_at=now_iso())
    store.event("reducer_failed", join=join.id, parallel=node.id, branch=bid, fold=fold_index, situation=kind)
    return execution.fail(store, join.id, kind, TOP, event="node_failed", parallel_node=node.id,
                          branch_id=bid, fold=fold_index, **details).situation


# ---------------------------------------------------------------- merge and join completion

def _merge(store: RunStore, flow: Flow, node: Node) -> None:
    region = flow.regions[node.id]
    join = flow.nodes[region.join]
    with store.transaction() as st:
        par = st["parallel"][node.id]
        order = par["branch_order"]
        if par["kind"] == "fork":
            merged = {}
            for bid in order:
                merged.update({k: v for k, v in par["branches"][bid]["variables"].items() if k in region.produced})
        else:
            merged = {v: [par["branches"][bid]["variables"].get(v) for bid in order] for v in sorted(region.produced)}
        if join.summary_var:
            merged[join.summary_var] = par["join_state"]["summary"]
        par["join_state"].update(status="merged", merged_variables=merged, merged_at=now_iso())
        par["status"] = "merged"
        if st["nodes"][join.id]["status"] in ("pending", "running"):
            st["nodes"][join.id]["status"] = "pending"
    store.event("join_merged", join=join.id, parallel=node.id, branches=len(order), variables=sorted(merged))


def _complete_join(store: RunStore, flow: Flow, node: Node) -> dict | None:
    state = store.read()
    par = state["parallel"][node.id]
    join = flow.nodes[par["join"]]
    merged = par["join_state"]["merged_variables"] or {}

    def finalize(st: dict) -> None:
        p = st["parallel"][node.id]
        p["status"] = "completed"
        p["join_state"].update(status="completed", completed_at=now_iso())
        st["nodes"][node.id].update(status="completed", completed_at=now_iso())

    step = execution.route_and_commit(store, flow, join, TOP, merged, extra_commit=finalize)
    if step.kind == "decision":
        return step.situation
    store.event("join_completed", join=join.id, parallel=node.id, branches=len(par["branch_order"]),
                variables=sorted(merged))
    return None
