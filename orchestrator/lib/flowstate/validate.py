"""Static graph and dataflow checks, run before any run is created.

Availability: a variable is available at a node if it is a run input or builtin, or if
every path from start to that node passes a node that produces it. A node's own
sets_variables are pre-bound (to output paths) for its prompt and script, but not for
its working_dir or output path templates. Gates and conditions on an edge see the
source node's committed outputs.
"""

import re
from collections import deque

from .errors import FlowstateError
from .model import AGENT_BUILTIN_VARS, BUILTIN_VARS, PHASE3_RUNNERS, Flow
from .procs import script_argv
from .templating import placeholders, resolve_include

VAR_REF = re.compile(r"FLOWSTATE_VAR_([A-Za-z_][A-Za-z0-9_]*)")
ALL_BUILTINS = BUILTIN_VARS | AGENT_BUILTIN_VARS


def check(flow: Flow, issues, n_starts: int, n_dones: int) -> None:
    _structure(flow, issues, n_starts, n_dones)
    order = _topo_order(flow)
    if order is None:
        issues.add("cycle", "graph has a cycle; this runtime executes acyclic graphs only")
        return
    referenced = _references(flow, issues, _available(flow, order))
    for name in sorted(set(flow.variables) - referenced):
        if not any(name in flow.produced_vars(n) for n in flow.nodes):
            issues.add("unused_variable", "declared but never produced or referenced",
                       severity="warning", variable=name)
    used_schemas = {n.output_schema for n in flow.nodes.values()}
    for name in sorted(set(flow.output_schemas) - used_schemas):
        issues.add("unused_output_schema", "not referenced by any node", severity="warning",
                   output_schema=name)


def _structure(flow: Flow, issues, n_starts: int, n_dones: int) -> None:
    if n_starts != 1:
        issues.add("start_count", f"need exactly one start node (shape=Mdiamond), found {n_starts}")
    if n_dones != 1:
        issues.add("done_count", f"need exactly one done node (shape=Msquare), found {n_dones}")
    known = [e for e in flow.edges if e.source in flow.nodes and e.target in flow.nodes]
    if flow.start:
        if flow.in_edges(flow.start):
            issues.add("invalid_connectivity", "start node has incoming edges", node=flow.start)
        if not flow.out_edges(flow.start):
            issues.add("invalid_connectivity", "start node has no outgoing edge", node=flow.start)
    if flow.done:
        if flow.out_edges(flow.done):
            issues.add("invalid_connectivity", "done node has outgoing edges", node=flow.done)
        if not flow.in_edges(flow.done):
            issues.add("invalid_connectivity", "done node has no incoming edge", node=flow.done)

    def reach(frm: str, forward: bool) -> set[str]:
        seen, queue = {frm}, deque([frm])
        while queue:
            cur = queue.popleft()
            for e in known:
                a, b = (e.source, e.target) if forward else (e.target, e.source)
                if a == cur and b not in seen:
                    seen.add(b)
                    queue.append(b)
        return seen

    if flow.start:
        for node_id in sorted(set(flow.nodes) - reach(flow.start, True)):
            issues.add("unreachable_node", "not reachable from start", node=node_id)
    if flow.done:
        for node_id in sorted(set(flow.nodes) - reach(flow.done, False)):
            issues.add("dead_end", "cannot reach done", node=node_id)

    for node in flow.nodes.values():
        outs = flow.out_edges(node.id)
        if len(outs) > 1 and node.kind not in PHASE3_RUNNERS and any(e.condition is None for e in outs):
            issues.add("ambiguous_branch", "a node with several outgoing edges needs a condition on "
                       "every edge (parallel fan-out is runner=fork)", node=node.id)
        if node.output_schema and node.output_schema not in flow.output_schemas:
            issues.add("unknown_output_schema", f"output_schema {node.output_schema!r} is not in flow.yml",
                       node=node.id)
        if node.kind in ("start", "done") and node.output_schema:
            issues.add("invalid_attribute", f"{node.kind} nodes cannot declare outputs", node=node.id)


def _topo_order(flow: Flow) -> list[str] | None:
    indeg = {n: 0 for n in flow.nodes}
    edges = [e for e in flow.edges if e.source in flow.nodes and e.target in flow.nodes]
    for e in edges:
        indeg[e.target] += 1
    queue = deque(sorted(n for n, d in indeg.items() if d == 0))
    order = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for e in edges:
            if e.source == cur:
                indeg[e.target] -= 1
                if indeg[e.target] == 0:
                    queue.append(e.target)
    return order if len(order) == len(flow.nodes) else None


def _available(flow: Flow, order: list[str]) -> dict[str, set[str]]:
    base = set(flow.input_vars) | BUILTIN_VARS
    avail: dict[str, set[str]] = {}
    for node_id in order:
        preds = [e.source for e in flow.in_edges(node_id) if e.source in avail]
        if node_id == flow.start or not preds:
            avail[node_id] = set(base)
        elif flow.nodes[node_id].kind == "join":
            # A join waits for every incoming branch, so it sees all of their outputs.
            avail[node_id] = set.union(*(avail[p] | flow.produced_vars(p) for p in preds))
        else:
            avail[node_id] = set.intersection(*(avail[p] | flow.produced_vars(p) for p in preds))
    return avail


def _references(flow: Flow, issues, avail: dict[str, set[str]]) -> set[str]:
    referenced: set[str] = set()
    declared = set(flow.variables)

    def need(names, allowed, where, **ctx):
        for name in sorted(names):
            referenced.add(name)
            if name not in declared and name not in ALL_BUILTINS:
                issues.add("undeclared_variable", f"{where} uses undeclared variable {name!r}", **ctx)
            elif name not in allowed:
                issues.add("variable_not_available",
                           f"{where} uses {name!r}, which is not set on every path to this point", **ctx)

    for node in flow.nodes.values():
        here = avail.get(node.id, set())
        schema = flow.output_schemas.get(node.output_schema)
        # Only path bindings exist before the node runs; pointer values need the output.
        own = {b.var for b in schema.bindings if b.pointer is None} if schema else set()
        builtins = BUILTIN_VARS | (AGENT_BUILTIN_VARS if node.kind == "agent" else set())
        if node.prompt_template and node.prompt_template.is_file():
            names, includes = placeholders(node.prompt_template.read_text())
            need(names, here | own | builtins, "prompt_template", node=node.id)
            for rel in includes:
                try:
                    path = resolve_include(flow.dir, rel)
                except FlowstateError as exc:
                    issues.add(exc.code, exc.message, node=node.id)
                    continue
                if not path.is_file():
                    issues.add("missing_file", f"include not found: {rel}", node=node.id, file=str(path))
        if node.working_dir:
            need(placeholders(node.working_dir)[0], here | builtins, "working_dir", node=node.id)
        if node.output_schema in flow.output_schemas:
            for f in flow.output_schemas[node.output_schema].files:
                need(placeholders(f.path)[0], here | builtins, f"output path of {f.name!r}", node=node.id)
        if node.script and node.script.is_file():
            _check_executable(node.script, issues, node=node.id)
            need(set(VAR_REF.findall(node.script.read_text(errors="replace"))), here | own | builtins,
                 "script", node=node.id)

    for edge in flow.edges:
        if edge.source not in flow.nodes:
            continue
        allowed = avail.get(edge.source, set()) | flow.produced_vars(edge.source) | BUILTIN_VARS
        for gate in edge.gates:
            if gate.is_file():
                _check_executable(gate, issues, edge=edge.id)
                need(set(VAR_REF.findall(gate.read_text(errors="replace"))), allowed, f"gate {gate.name}",
                     edge=edge.id)
        if edge.condition_tree is not None:
            from .conditions import identifiers
            need(identifiers(edge.condition_tree), allowed, "condition", edge=edge.id)
    return referenced


def _check_executable(path, issues, **ctx) -> None:
    try:
        script_argv(path)
    except FlowstateError as exc:
        issues.add(exc.code, exc.message, **ctx)
