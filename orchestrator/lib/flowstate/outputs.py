"""Resolve, validate and bind a node's declared outputs.

A node may advance only when every declared file exists, parses as JSON, validates
against its schema, (agent nodes) carries the assigned `_session_id`, and every
sets_variables binding extracts a value of the declared type.
"""

import json
import shutil
from pathlib import Path

import jsonschema

from .errors import FlowstateError
from .model import Flow, Node
from .templating import render

MAX_SCHEMA_ERRORS_PER_FILE = 20


def resolve_paths(flow: Flow, node: Node, variables: dict, run_dir: Path) -> dict[str, Path]:
    if not node.output_schema:
        return {}
    out = {}
    for f in flow.output_schemas[node.output_schema].files:
        path = Path(render(f.path, variables, flow.dir, f"output path of {f.name!r}"))
        if not path.is_absolute():
            path = Path(variables["_run_artefact_dir"]) / path
        path = path.resolve()
        if run_dir not in path.parents:
            raise FlowstateError("output_outside_run", f"output {f.name!r} resolves outside the run: {path}",
                                 {"node": node.id})
        out[f.name] = path
    return out


def prebound(flow: Flow, node: Node, paths: dict[str, Path]) -> dict[str, str]:
    if not node.output_schema:
        return {}
    return {b.var: str(paths[b.file]) for b in flow.output_schemas[node.output_schema].bindings
            if b.pointer is None}


def validate(flow: Flow, node: Node, paths: dict[str, Path],
             session_id: str | None) -> tuple[list[dict], dict]:
    errors, docs = [], {}
    if not node.output_schema:
        return errors, docs
    for f in flow.output_schemas[node.output_schema].files:
        path = paths[f.name]
        where = {"file": f.name, "path": str(path)}
        if not path.is_file():
            errors.append({**where, "error": "missing", "message": "declared output file does not exist"})
            continue
        try:
            doc = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append({**where, "error": "invalid_json", "message": str(exc)})
            continue
        validator = jsonschema.validators.validator_for(f.schema)(f.schema)
        schema_errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
        for e in schema_errors[:MAX_SCHEMA_ERRORS_PER_FILE]:
            errors.append({**where, "error": "schema", "at": "/" + "/".join(str(p) for p in e.absolute_path),
                           "message": e.message})
        if session_id is not None:
            actual = doc.get("_session_id") if isinstance(doc, dict) else None
            if actual != session_id:
                errors.append({**where, "error": "session_id_mismatch", "expected": session_id,
                               "actual": actual, "message": "_session_id must equal the session id "
                               "flowstate assigned to this node's worker"})
        docs[f.name] = doc
    return errors, docs


def json_pointer(doc, pointer: str):
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    cur = doc
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(pointer)
    return cur


TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "path": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "dict": lambda v: isinstance(v, dict),
    "list": lambda v: isinstance(v, list),
    "any": lambda v: True,
}


def bind(flow: Flow, node: Node, paths: dict[str, Path], docs: dict) -> tuple[dict, list[dict]]:
    values, errors = {}, []
    if not node.output_schema:
        return values, errors
    for b in flow.output_schemas[node.output_schema].bindings:
        if b.pointer is None:
            value = str(paths[b.file])
        else:
            try:
                value = json_pointer(docs[b.file], b.pointer)
            except (KeyError, IndexError, ValueError, TypeError):
                errors.append({"error": "binding_failed", "variable": b.var, "file": b.file,
                               "message": f"pointer {b.pointer!r} not found in output"})
                continue
        vtype = flow.variables[b.var].type
        if not TYPE_CHECKS[vtype](value):
            errors.append({"error": "binding_type", "variable": b.var, "file": b.file,
                           "message": f"expected {vtype}, got {type(value).__name__}"})
            continue
        values[b.var] = value
    return values, errors


def snapshot(paths: dict[str, Path], dest: Path) -> list[str]:
    """Preserve rejected outputs before anything can overwrite them."""
    saved = []
    for name, path in paths.items():
        if path.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / f"{name}{path.suffix}"
            shutil.copy2(path, target)
            saved.append(str(target))
    return saved
