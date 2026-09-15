"""Load <flow>.dot (pydot) and <flow>.flow.yml into a Flow, collecting every issue.

`check_flow` never raises for flow problems; `load_flow` raises one invalid_flow error
listing all of them. Static validation therefore happens before any run exists.
"""

import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pydot
import yaml

from . import conditions, validate
from .errors import FlowstateError
from .model import (PARALLEL_KINDS, RESERVED_VARS, VAR_TYPES, Edge, Flow, Node, OutputFile, OutputSchema,
                    VarBinding, VariableDecl)
from .templating import placeholders, resolve_include

PSEUDO_NODES = {"graph", "node", "edge"}
NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")  # ids appear in worker ids and paths
COSMETIC = {"label", "color", "fillcolor", "style", "fontname", "fontsize", "fontcolor",
            "tooltip", "penwidth", "xlabel", "width", "height", "group", "comment"}
GRAPH_ATTRS = COSMETIC | {"description", "rankdir", "bgcolor", "splines", "nodesep", "ranksep",
                          "labelloc", "labeljust", "compound", "newrank", "concentrate"}
EDGE_ATTRS = COSMETIC | {"arrowhead", "arrowsize", "weight", "constraint", "gates", "condition"}
EXEC_COMMON = {"description", "working_dir", "output_schema", "pause_at", "max_retries", "timeout"}
ATTRS_BY_KIND = {
    "start": COSMETIC | {"shape", "description"},
    "done": COSMETIC | {"shape", "description"},
    "agent": COSMETIC | EXEC_COMMON | {"shape", "runner", "prompt_template", "model",
                                       "permission_mode", "stall_after", "max_budget_usd",
                                       "harness", "add_dirs"},
    "script": COSMETIC | EXEC_COMMON | {"shape", "runner", "script"},
    "fork": COSMETIC | {"shape", "runner", "description", "max_parallel"},
    "dynamic_fanout": COSMETIC | {"shape", "runner", "description", "items", "max_items", "max_parallel"},
    "join": COSMETIC | {"shape", "runner", "description", "reducer_script", "summary_var", "max_retries"},
}
PAUSE_AT = ("never", "optional", "always")
PERMISSION_MODES = ("acceptEdits", "auto", "manual", "dontAsk", "plan")
HARNESSES = ("claude", "fake")


class Issues(list):
    def add(self, code: str, message: str, severity: str = "error", **context) -> None:
        self.append({"severity": severity, "code": code, "message": message,
                     **{k: v for k, v in context.items() if v is not None}})

    @property
    def errors(self) -> list[dict]:
        return [i for i in self if i["severity"] == "error"]


def _unquote(value) -> str:
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def _attrs(obj) -> dict[str, str]:
    return {k: _unquote(v) for k, v in obj.get_attributes().items()}


def _number(node_id, attrs, key, issues, cast=float, minimum=0.0, strict=True):
    if key not in attrs:
        return None
    try:
        value = cast(attrs[key])
    except ValueError:
        issues.add("invalid_attribute", f"{key}={attrs[key]!r} is not a {cast.__name__}", node=node_id)
        return None
    if value < minimum or (strict and value == minimum):
        issues.add("invalid_attribute", f"{key} must be {'>' if strict else '>='} {minimum}", node=node_id)
        return None
    return value


def _kind(node_id: str, attrs: dict, issues: Issues) -> str | None:
    shape, runner = attrs.get("shape"), attrs.get("runner")
    if shape == "Mdiamond":
        return "start"
    if shape == "Msquare":
        return "done"
    if runner in PARALLEL_KINDS or runner == "join":
        return runner
    if runner == "script":
        return "script"
    if runner == "agent" or (runner is None and "prompt_template" in attrs):
        return "agent"
    if runner is not None:
        issues.add("unknown_runner", f"unknown runner {runner!r}", node=node_id)
    else:
        issues.add("unknown_node_type",
                   "node is not start (Mdiamond), done (Msquare), an agent (prompt_template) "
                   "or a runner=script/fork/join/dynamic_fanout node", node=node_id)
    return None


def _flow_file(flow_dir: Path, rel: str, what: str, issues: Issues, **ctx) -> Path:
    path = (flow_dir / rel).resolve()
    if not path.is_file():
        issues.add("missing_file", f"{what} not found: {rel}", file=str(path), **ctx)
    return path


def _build_node(node_id: str, attrs: dict, flow_dir: Path, issues: Issues) -> Node | None:
    if not NODE_ID_RE.match(node_id):
        issues.add("invalid_node_id", "node ids must start with a letter and use letters, digits, '_' or '-'",
                   node=node_id)
    kind = _kind(node_id, attrs, issues)
    if kind is None:
        return None
    for key in sorted(set(attrs) - ATTRS_BY_KIND[kind]):
        issues.add("unsupported_attribute", f"attribute {key!r} is not valid on a {kind} node",
                   node=node_id)
    node = Node(id=node_id, kind=kind, attrs=attrs, description=attrs.get("description"),
                working_dir=attrs.get("working_dir"), output_schema=attrs.get("output_schema"),
                model=attrs.get("model"), permission_mode=attrs.get("permission_mode"),
                harness=attrs.get("harness"))
    if kind in PARALLEL_KINDS:
        node.max_parallel = _number(node_id, attrs, "max_parallel", issues, int, 1, strict=False)
    if kind == "dynamic_fanout":
        node.items = attrs.get("items") or None
        if not node.items:
            issues.add("missing_attribute", "dynamic_fanout needs items=<variable holding a JSON list>",
                       node=node_id)
        node.max_items = _number(node_id, attrs, "max_items", issues, int, 1, strict=False)
    if kind == "join":
        if "reducer_script" in attrs:
            node.reducer_script = _flow_file(flow_dir, attrs["reducer_script"], "reducer script", issues,
                                             node=node_id)
        node.summary_var = attrs.get("summary_var") or None
        if bool(node.reducer_script) != bool(node.summary_var):
            issues.add("invalid_join", "reducer_script and summary_var must be given together "
                       "(the reducer maintains summary_var)", node=node_id)
        node.max_retries = _number(node_id, attrs, "max_retries", issues, int, 0, strict=False)
    if kind in PARALLEL_KINDS or kind == "join":
        return node
    if kind == "agent":
        if "prompt_template" not in attrs:
            issues.add("missing_attribute", "agent node needs prompt_template", node=node_id)
        else:
            node.prompt_template = _flow_file(flow_dir, attrs["prompt_template"], "prompt template",
                                              issues, node=node_id)
        if not node.output_schema:
            issues.add("missing_attribute", "agent node needs output_schema (outputs prove the "
                       "work and carry _session_id)", node=node_id)
    if kind == "script":
        if "script" not in attrs:
            issues.add("missing_attribute", "runner=script node needs script", node=node_id)
        else:
            node.script = _flow_file(flow_dir, attrs["script"], "script", issues, node=node_id)
    if kind in ("agent", "script"):
        node.pause_at = attrs.get("pause_at", "never")
        if node.pause_at not in PAUSE_AT:
            issues.add("invalid_attribute", f"pause_at must be one of {PAUSE_AT}", node=node_id)
        node.max_retries = _number(node_id, attrs, "max_retries", issues, int, 0, strict=False)
        node.timeout = _number(node_id, attrs, "timeout", issues)
    if kind == "agent":
        node.stall_after = _number(node_id, attrs, "stall_after", issues)
        node.max_budget_usd = _number(node_id, attrs, "max_budget_usd", issues)
        if node.permission_mode and node.permission_mode not in PERMISSION_MODES:
            issues.add("invalid_attribute", f"permission_mode must be one of {PERMISSION_MODES}",
                       node=node_id)
        if node.harness and node.harness not in HARNESSES:
            issues.add("invalid_attribute", f"harness must be one of {HARNESSES}", node=node_id)
        node.add_dirs = [d.strip() for d in attrs.get("add_dirs", "").split(",") if d.strip()]
    return node


def _load_yml(yml_path: Path, flow_dir: Path, issues: Issues):
    variables, schemas = {}, {}
    if not yml_path.is_file():
        issues.add("missing_file", f"flow.yml not found: {yml_path.name}", file=str(yml_path))
        return variables, schemas
    try:
        data = yaml.safe_load(yml_path.read_text()) or {}
    except yaml.YAMLError as exc:
        issues.add("yaml_error", str(exc), file=str(yml_path))
        return variables, schemas
    if not isinstance(data, dict):
        issues.add("yaml_error", "flow.yml must be a mapping", file=str(yml_path))
        return variables, schemas
    for key in sorted(set(data) - {"output_schemas", "variables", "description", "digest_include"}):
        issues.add("unsupported_key", f"unknown top-level key {key!r}", file=str(yml_path))

    for name, spec in (data.get("variables") or {}).items():
        spec = spec or {}
        if not isinstance(spec, dict):
            issues.add("invalid_variable", "variable spec must be a mapping", variable=name)
            continue
        if name in RESERVED_VARS:
            issues.add("reserved_variable", f"{name!r} is reserved for the dynamic_fanout item", variable=name)
        for key in sorted(set(spec) - {"type", "required", "default", "description"}):
            issues.add("unsupported_key", f"unknown key {key!r}", variable=name)
        vtype = spec.get("type", "string")
        if vtype not in VAR_TYPES:
            issues.add("invalid_variable", f"type must be one of {sorted(VAR_TYPES)}", variable=name)
        variables[name] = VariableDecl(name=name, type=vtype, required=bool(spec.get("required", True)),
                                       has_default="default" in spec, default=spec.get("default"),
                                       description=spec.get("description"))

    for sname, spec in (data.get("output_schemas") or {}).items():
        if not isinstance(spec, dict):
            issues.add("invalid_output_schema", "must be a mapping", output_schema=sname)
            continue
        for key in sorted(set(spec) - {"files", "sets_variables"}):
            issues.add("unsupported_key", f"unknown key {key!r}", output_schema=sname)
        files = []
        for f in spec.get("files") or []:
            if not isinstance(f, dict) or not {"name", "path", "definition"} <= set(f):
                issues.add("invalid_output_schema", "each file needs name, path and definition",
                           output_schema=sname)
                continue
            for key in sorted(set(f) - {"name", "path", "definition"}):
                issues.add("unsupported_key", f"unknown key {key!r}", output_schema=sname, file=f["name"])
            schema_path = _flow_file(flow_dir, f"definitions/{f['definition']}.json", "schema definition",
                                     issues, output_schema=sname)
            schema = {}
            if schema_path.is_file():
                try:
                    schema = json.loads(schema_path.read_text())
                    jsonschema.validators.validator_for(schema).check_schema(schema)
                except (json.JSONDecodeError, jsonschema.SchemaError) as exc:
                    issues.add("invalid_schema", str(exc).splitlines()[0], file=str(schema_path))
            files.append(OutputFile(name=f["name"], path=str(f["path"]), definition=f["definition"],
                                    schema_path=schema_path, schema=schema))
        if not files:
            issues.add("invalid_output_schema", "declares no files", output_schema=sname)
        names = [f.name for f in files]
        if len(names) != len(set(names)):
            issues.add("invalid_output_schema", "duplicate file names", output_schema=sname)
        bindings = []
        for var, source in (spec.get("sets_variables") or {}).items():
            if isinstance(source, str):
                binding = VarBinding(var=var, file=source)
            elif isinstance(source, dict) and set(source) <= {"file", "pointer"} and "file" in source:
                binding = VarBinding(var=var, file=source["file"], pointer=source.get("pointer"))
            else:
                issues.add("invalid_output_schema", "sets_variables values are a file name or "
                           "{file, pointer}", output_schema=sname, variable=var)
                continue
            if binding.file not in names:
                issues.add("invalid_output_schema", f"sets_variables refers to unknown file "
                           f"{binding.file!r}", output_schema=sname, variable=var)
            if var not in variables:
                issues.add("undeclared_variable", "sets_variables target is not declared under variables",
                           output_schema=sname, variable=var)
            bindings.append(binding)
        schemas[sname] = OutputSchema(name=sname, files=files, bindings=bindings)
    return variables, schemas


def _digest_include(yml_path: Path, flow_dir: Path, issues: Issues) -> list[Path]:
    """flow.yml `digest_include`: glob patterns, relative to the flow directory, for files the flow's scripts
    and gates depend on without naming them as node attributes (an imported package, config files). They
    join the flow digest, so changing one after init is reported as flow_changed."""
    try:
        data = yaml.safe_load(yml_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []  # already reported by _load_yml
    patterns = data.get("digest_include") if isinstance(data, dict) else None
    if patterns is None:
        return []
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p.strip() for p in patterns):
        issues.add("invalid_digest_include", "digest_include must be a list of glob patterns", file=str(yml_path))
        return []
    base, files = flow_dir.resolve(), []
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            issues.add("invalid_digest_include", f"pattern {pattern!r} must stay inside the flow directory",
                       file=str(yml_path))
            continue
        matched = sorted(p for p in base.glob(pattern) if p.is_file() and "__pycache__" not in p.parts)
        if not matched:
            issues.add("invalid_digest_include", f"pattern {pattern!r} matches no files", file=str(yml_path))
        files += matched
    return files


def _digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted({p for p in paths if p and p.is_file()}):
        h.update(str(p).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def check_flow(dot_path: Path, yml_path: Path) -> tuple[Flow | None, Issues]:
    issues = Issues()
    flow_dir = dot_path.parent
    try:
        graphs = pydot.graph_from_dot_file(str(dot_path))
    except Exception as exc:  # pydot raises assorted parser exceptions
        graphs = None
        issues.add("dot_parse_error", str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                   file=str(dot_path))
    if not graphs:
        if not issues:
            issues.add("dot_parse_error", "no graph found", file=str(dot_path))
        return None, issues
    if len(graphs) != 1:
        issues.add("dot_parse_error", "exactly one graph per DOT file", file=str(dot_path))
    graph = graphs[0]
    if graph.get_type() != "digraph":
        issues.add("dot_parse_error", "graph must be a digraph", file=str(dot_path))
    if graph.get_subgraphs():
        issues.add("unsupported_syntax", "subgraphs/clusters are not supported", file=str(dot_path))

    graph_attrs = _attrs(graph)
    raw_nodes: dict[str, dict] = {}
    for n in graph.get_nodes():
        name = _unquote(n.get_name())
        if name == "graph":
            graph_attrs.update(_attrs(n))
        elif name in PSEUDO_NODES:
            issues.add("unsupported_syntax", f"`{name} [...]` defaults are not supported; set attributes "
                       "on each node/edge", file=str(dot_path))
        else:
            raw_nodes[name] = _attrs(n)
    for key in sorted(set(graph_attrs) - GRAPH_ATTRS):
        issues.add("unsupported_attribute", f"graph attribute {key!r} is not supported")

    variables, schemas = _load_yml(yml_path, flow_dir, issues)

    nodes = {}
    for node_id, attrs in raw_nodes.items():
        node = _build_node(node_id, attrs, flow_dir, issues)
        if node:
            nodes[node_id] = node

    edges = []
    for e in graph.get_edges():
        src, dst = _unquote(e.get_source()), _unquote(e.get_destination())
        attrs = _attrs(e)
        edge = Edge(source=src, target=dst, attrs=attrs)
        for key in sorted(set(attrs) - EDGE_ATTRS):
            issues.add("unsupported_attribute", f"edge attribute {key!r} is not supported", edge=edge.id)
        for node_id in (src, dst):
            if node_id not in raw_nodes:
                issues.add("undeclared_node", f"edge references undeclared node {node_id!r}", edge=edge.id)
        for rel in [g.strip() for g in attrs.get("gates", "").split(",") if g.strip()]:
            edge.gates.append(_flow_file(flow_dir, rel, "gate", issues, edge=edge.id))
        if attrs.get("condition") is not None:
            edge.condition = attrs["condition"]
            try:
                edge.condition_tree = conditions.parse(edge.condition)
            except FlowstateError as exc:
                issues.add("invalid_condition", exc.message, edge=edge.id, condition=edge.condition)
        edges.append(edge)

    starts = [n.id for n in nodes.values() if n.kind == "start"]
    dones = [n.id for n in nodes.values() if n.kind == "done"]
    produced = {v for n in nodes.values() if n.output_schema in schemas
                for v in schemas[n.output_schema].produced_vars()}
    produced |= {n.summary_var for n in nodes.values() if n.kind == "join" and n.summary_var}

    referenced = [dot_path, yml_path]
    referenced += _digest_include(yml_path, flow_dir, issues)
    referenced += [f.schema_path for s in schemas.values() for f in s.files]
    referenced += [n.prompt_template for n in nodes.values()] + [n.script for n in nodes.values()]
    referenced += [n.reducer_script for n in nodes.values()]
    referenced += [g for e in edges for g in e.gates]
    for n in nodes.values():
        if n.prompt_template and n.prompt_template.is_file():
            for rel in placeholders(n.prompt_template.read_text())[1]:
                try:
                    referenced.append(resolve_include(flow_dir, rel))
                except FlowstateError:
                    pass

    flow = Flow(name=dot_path.stem, dir=flow_dir, dot_path=dot_path, yml_path=yml_path,
                graph_name=graph.get_name(), label=graph_attrs.get("label"), nodes=nodes, edges=edges,
                variables=variables, output_schemas=schemas,
                start=starts[0] if len(starts) == 1 else None, done=dones[0] if len(dones) == 1 else None,
                digest=_digest(referenced), input_vars=set(variables) - produced)
    validate.check(flow, issues, n_starts=len(starts), n_dones=len(dones))
    return flow, issues


def load_flow(dot_path: Path, yml_path: Path) -> Flow:
    flow, issues = check_flow(dot_path, yml_path)
    if issues.errors or flow is None:
        raise FlowstateError("invalid_flow", f"{dot_path.name}: {len(issues.errors)} error(s)",
                             {"issues": issues})
    return flow
