"""Static graph and dataflow checks, run before any run is created.

Availability: a variable is available at a node if it is a run input or builtin, or if
every path from start to that node passes a node that produces it. A node's own path
bindings are pre-bound for its prompt and script, but not for its working_dir or output
path templates. Gates and conditions on an edge see the source node's committed outputs.

Regions: every fork/dynamic_fanout owns the nodes between it and exactly one join. Branches
may not leave the region, be entered from outside it, or contain another fork/fan-out.
Fork branches are disjoint and produce disjoint variables. Inside a region, `_branch_id`,
`_branch_index`, `_parallel_node` (and `item` for dynamic_fanout) are available. At the join,
a fork contributes each branch's guaranteed exit variables; a fan-out contributes every
region variable (as a list in branch order).
"""

import re
from collections import deque

from .errors import FlowstateError
from .model import (AGENT_BUILTIN_VARS, BRANCH_BUILTIN_VARS, BUILTIN_VARS, ITEM_VAR, PARALLEL_KINDS, Flow,
                    Region)
from .procs import script_argv
from .templating import placeholders, resolve_include

VAR_REF = re.compile(r"FLOWSTATE_VAR_([A-Za-z_][A-Za-z0-9_]*)")
REGION_ONLY = BRANCH_BUILTIN_VARS | {ITEM_VAR}
ALL_BUILTINS = BUILTIN_VARS | AGENT_BUILTIN_VARS | REGION_ONLY


def check(flow: Flow, issues, n_starts: int, n_dones: int) -> None:
    _structure(flow, issues, n_starts, n_dones)
    order = _topo_order(flow)
    if order is None:
        issues.add("cycle", "graph has a cycle; this runtime executes acyclic graphs only")
        return
    _regions(flow, issues)
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
        if len(outs) > 1 and node.kind not in PARALLEL_KINDS and any(e.condition is None for e in outs):
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


def _regions(flow: Flow, issues) -> None:
    joins_reached: dict[str, list[str]] = {}
    for p in sorted((n for n in flow.nodes.values() if n.kind in PARALLEL_KINDS), key=lambda n: n.id):
        outs = [e for e in flow.out_edges(p.id) if e.target in flow.nodes]
        for e in outs:
            if e.condition is not None or e.gates:
                issues.add("invalid_branch_edge", "edges leaving a fork/dynamic_fanout cannot carry "
                           "conditions or gates", edge=e.id)
        if p.kind == "fork" and len(outs) < 2:
            issues.add("invalid_fork", "a fork needs at least two outgoing edges", node=p.id)
        if p.kind == "dynamic_fanout" and len(outs) != 1:
            issues.add("invalid_fanout", "a dynamic_fanout needs exactly one outgoing edge (the branch "
                       "template)", node=p.id)
        if not outs:
            continue

        ok, branches, joins = True, {}, set()
        for e in sorted(outs, key=lambda e: e.target):
            if flow.nodes[e.target].kind == "join":
                issues.add("empty_branch", "a branch needs at least one node before its join", edge=e.id)
                ok = False
                continue
            seen, stack = set(), [e.target]
            while stack:
                cur = stack.pop()
                node = flow.nodes.get(cur)
                if cur in seen or node is None:
                    continue
                if node.kind == "join":
                    joins.add(cur)
                    continue
                if node.kind in ("start", "done"):
                    issues.add("branch_escapes", f"a branch of {p.id} reaches {cur} without passing a join",
                               node=p.id)
                    ok = False
                    continue
                if node.kind in PARALLEL_KINDS:
                    issues.add("nested_parallel", f"{cur} is inside the region of {p.id}; nested "
                               "fork/dynamic_fanout is not supported", node=cur)
                    ok = False
                    continue
                seen.add(cur)
                stack.extend(x.target for x in flow.out_edges(cur))
            branches[e.target] = seen
        for j in joins:
            joins_reached.setdefault(j, []).append(p.id)
        if len(joins) != 1:
            issues.add("unmatched_join", f"all branches of {p.id} must end at exactly one join, found "
                       f"{sorted(joins) or 'none'}", node=p.id)
            ok = False
        if not ok:
            continue
        join = next(iter(joins))

        members = [n for b in branches.values() for n in b]
        if p.kind == "fork" and len(members) != len(set(members)):
            issues.add("overlapping_branches", "fork branches must not share nodes", node=p.id)
            continue
        region_nodes = set(members)
        for n in sorted(region_nodes):
            for e in flow.in_edges(n):
                if e.source not in region_nodes and e.source != p.id:
                    issues.add("branch_entered_from_outside", f"{e.source} enters the region of {p.id} "
                               "without passing through it", edge=e.id)
        for e in flow.in_edges(join):
            if e.source not in region_nodes:
                issues.add("invalid_join", f"join {join} has an input from {e.source}, outside the region "
                           f"of {p.id}", edge=e.id)
        for entry, nodes in sorted(branches.items()):
            if not any(e.target == join for n in nodes for e in flow.out_edges(n)):
                issues.add("branch_escapes", f"the branch starting at {entry} never reaches join {join}",
                           node=p.id)

        produced = {v for n in region_nodes for v in flow.produced_vars(n)}
        if p.kind == "fork":
            owner: dict[str, str] = {}
            for entry, nodes in sorted(branches.items()):
                for v in sorted({v for n in nodes for v in flow.produced_vars(n)}):
                    if v in owner:
                        issues.add("branch_variable_conflict", f"{v!r} is produced by branches {owner[v]} and "
                                   f"{entry}; fork outputs merge by name", node=p.id)
                    owner.setdefault(v, entry)
        outside = {v for n in flow.nodes if n not in region_nodes for v in flow.produced_vars(n)}
        for v in sorted(produced & outside):
            issues.add("branch_variable_conflict", f"{v!r} is produced both inside the region of {p.id} "
                       "and outside it", node=p.id)
        if p.kind == "dynamic_fanout" and p.items:
            if p.items not in flow.variables:
                issues.add("undeclared_variable", f"items names undeclared variable {p.items!r}", node=p.id)
            elif flow.variables[p.items].type not in ("list", "path", "any"):
                issues.add("invalid_fanout", "items must name a list variable (or a path to a JSON list file)",
                           node=p.id)
        flow.regions[p.id] = Region(parallel=p.id, kind=p.kind, join=join, nodes=region_nodes,
                                    branches=branches, produced=produced)

    for node in flow.nodes.values():
        if node.kind != "join":
            continue
        closers = joins_reached.get(node.id, [])
        if not closers:
            issues.add("orphan_join", "join does not close any fork or dynamic_fanout", node=node.id)
        elif len(closers) > 1:
            issues.add("invalid_join", f"join {node.id} closes several regions: {sorted(closers)}", node=node.id)
        if node.summary_var:
            decl = flow.variables.get(node.summary_var)
            if decl is None:
                issues.add("undeclared_variable", f"summary_var {node.summary_var!r} is not declared", node=node.id)
            elif decl.type not in ("dict", "any"):
                issues.add("invalid_join", "summary_var must be a dict variable", node=node.id)


def _available(flow: Flow, order: list[str]) -> dict[str, set[str]]:
    base = set(flow.input_vars) | BUILTIN_VARS
    avail: dict[str, set[str]] = {}
    for node_id in order:
        node = flow.nodes[node_id]
        preds = [e.source for e in flow.in_edges(node_id) if e.source in avail]
        region = flow.region_for_join(node_id) if node.kind == "join" else None
        if node_id == flow.start or not preds:
            avail[node_id] = set(base)
        elif region is not None:
            parent = avail[region.parallel]
            for entry, nodes in region.branches.items():
                ins = [e.source for e in flow.in_edges(node_id) if e.source in nodes]
                region.exit_vars[entry] = (set.intersection(*(avail[s] | flow.produced_vars(s) for s in ins))
                                           if ins else set(parent))
            if region.kind == "dynamic_fanout":
                avail[node_id] = parent | region.produced
            else:
                avail[node_id] = parent | set().union(*region.exit_vars.values())
        elif node.kind == "join":
            avail[node_id] = set.union(*(avail[p] | flow.produced_vars(p) for p in preds))
        else:
            avail[node_id] = set.intersection(*(avail[p] | flow.produced_vars(p) for p in preds))
    return avail


def _region_builtins(flow: Flow, node_id: str) -> set[str]:
    region = flow.region_of(node_id)
    if region is None:
        return set()
    return BRANCH_BUILTIN_VARS | ({ITEM_VAR} if region.kind == "dynamic_fanout" else set())


def _references(flow: Flow, issues, avail: dict[str, set[str]]) -> set[str]:
    referenced: set[str] = set()
    declared = set(flow.variables)

    def need(names, allowed, where, **ctx):
        for name in sorted(names):
            referenced.add(name)
            if name not in declared and name not in ALL_BUILTINS:
                issues.add("undeclared_variable", f"{where} uses undeclared variable {name!r}", **ctx)
            elif name in REGION_ONLY and name not in allowed:
                issues.add("branch_variable_outside_region", f"{where} uses {name!r}, which only exists "
                           "inside a fork/dynamic_fanout region" + (" (item: dynamic_fanout only)"
                                                                    if name == ITEM_VAR else ""), **ctx)
            elif name not in allowed:
                issues.add("variable_not_available",
                           f"{where} uses {name!r}, which is not set on every path to this point", **ctx)

    for node in flow.nodes.values():
        here = avail.get(node.id, set())
        schema = flow.output_schemas.get(node.output_schema)
        # Only path bindings exist before the node runs; pointer values need the output.
        own = {b.var for b in schema.bindings if b.pointer is None} if schema else set()
        builtins = BUILTIN_VARS | (AGENT_BUILTIN_VARS if node.kind == "agent" else set()) \
            | _region_builtins(flow, node.id)
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
        if schema:
            for f in schema.files:
                need(placeholders(f.path)[0], here | builtins, f"output path of {f.name!r}", node=node.id)
        if node.script and node.script.is_file():
            _check_executable(node.script, issues, node=node.id)
            need(set(VAR_REF.findall(node.script.read_text(errors="replace"))), here | own | builtins,
                 "script", node=node.id)
        if node.kind == "dynamic_fanout" and node.items and node.items in declared:
            need({node.items}, here, "items", node=node.id)
        region = flow.region_for_join(node.id) if node.kind == "join" else None
        if node.reducer_script and node.reducer_script.is_file():
            _check_executable(node.reducer_script, issues, node=node.id)
            allowed = BUILTIN_VARS | (avail.get(region.parallel, set()) | region.produced
                                      | _region_builtins(flow, next(iter(region.nodes)))
                                      if region else here)
            need(set(VAR_REF.findall(node.reducer_script.read_text(errors="replace"))), allowed,
                 "reducer_script", node=node.id)

    for edge in flow.edges:
        if edge.source not in flow.nodes:
            continue
        allowed = avail.get(edge.source, set()) | flow.produced_vars(edge.source) | BUILTIN_VARS \
            | _region_builtins(flow, edge.source)
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
