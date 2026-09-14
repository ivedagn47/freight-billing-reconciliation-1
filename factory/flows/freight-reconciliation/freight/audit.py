"""Audit what a worker actually did, from its own transcript.

Prompts tell workers which files to read; this checks they did. Every tool call in the worker's
stream-json transcript (all invocations, including retries) is inspected:

  - only the file tools are allowed (Read, Glob, Grep, Write, Edit);
  - reads and searches must target an assigned input file or the worker's own workspace (its branch
    artefact directory);
  - writes must stay inside the workspace.

This is what makes "two independent extractions" checkable: neither extraction worker can have
read the other's rate spec, a cached spec, or anything else besides its contract and clause index.
The worker is identified by the `_session_id` its outputs carry (already proven by flowstate to be
the session it assigned). A violation cannot be undone by retrying the same conversation; the worker
must be respawned.
"""

import json
from pathlib import Path

FILE_TOOLS = {"Read": ("read", "file_path"), "Glob": ("search", "path"), "Grep": ("search", "path"),
              "Write": ("write", "file_path"), "Edit": ("write", "file_path")}
GLOB_CHARS = set("*?[{")


class AuditError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def output_session_ids(workspace: Path) -> set[str]:
    ids = set()
    for path in sorted(Path(workspace).glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("_session_id"), str):
            ids.add(doc["_session_id"])
    return ids


def find_worker(workers_dir: Path, session_id: str) -> Path:
    matches = []
    for meta in sorted(Path(workers_dir).glob("*/meta.json")):
        try:
            if json.loads(meta.read_text()).get("session_id") == session_id:
                matches.append(meta.parent)
        except (OSError, json.JSONDecodeError):
            continue
    if len(matches) != 1:
        raise AuditError([f"expected one worker with session {session_id}, found {len(matches)}"])
    return matches[0]


def tool_uses(transcript: Path) -> list[dict]:
    uses = []
    try:
        raw_lines = Path(transcript).read_text().splitlines()
    except FileNotFoundError:
        return uses
    for raw in raw_lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append({"name": block.get("name"), "input": block.get("input") or {}})
    return uses


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _glob_base(pattern: str) -> str:
    parts = []
    for part in Path(pattern).parts:
        if GLOB_CHARS & set(part):
            break
        parts.append(part)
    return str(Path(*parts)) if parts else "/"


def audit(worker_dir: Path, allowed_files: list[str], workspace: Path) -> dict:
    meta = json.loads((Path(worker_dir) / "meta.json").read_text())
    cwd = Path(meta["cwd"]).resolve()
    allowed = {Path(p).resolve() for p in allowed_files}
    workspace = Path(workspace).resolve()
    calls, violations = [], []
    for use in tool_uses(Path(worker_dir) / "transcript.jsonl"):
        name, args = use["name"], use["input"]
        if name not in FILE_TOOLS:
            violations.append(f"used tool {name!r}; workers may use only {sorted(FILE_TOOLS)}")
            calls.append({"tool": name})
            continue
        action, key = FILE_TOOLS[name]
        raw = args.get(key)
        if name == "Glob" and str(args.get("pattern", "")).startswith("/"):
            raw = _glob_base(args["pattern"])
        target = (cwd / raw).resolve() if raw else cwd
        calls.append({"tool": name, "action": action, "path": str(target)})
        ok = _inside(target, workspace) or (action != "write" and target in allowed)
        if not ok:
            verb = "wrote" if action == "write" else "read"
            violations.append(f"{name} {verb} {target}, which is not one of its inputs")
    return {"worker": Path(worker_dir).name, "session_id": meta.get("session_id"), "tool_calls": calls,
            "violations": violations}
