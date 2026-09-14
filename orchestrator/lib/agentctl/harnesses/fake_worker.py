"""Deterministic stand-in for a Claude Code worker process.

Reads the prompt on stdin, executes a JSON script, and emits stream-json events
shaped like Claude Code's, so agentctl and flowstate can be tested without tokens.

Script format:
    {"model": "fake",
     "invocations": [            # indexed by invocation number; the last entry repeats
        {"steps": [
            {"capture": {"name": "out", "regex": "at exactly this path: (\\S+)"}},
            {"write": {"path": "${out}", "content": "text"}},     # or "json": {...}
            {"text": "assistant says ${session_id}"},
            {"sleep": 0.5},                                         # silent: looks stalled
            {"heartbeat": {"count": 3, "interval": 0.2}}            # periodic events
         ],
         "exit_code": 0, "is_error": false, "cost_usd": 0.0, "num_turns": 1,
         "result": "done"}]}

Templates use ${name}; available names: session_id, invocation, cwd, worker_id,
plus every capture. Captures match against the prompt text (group 1, else group 0).
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from string import Template


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _render(value, names: dict):
    if isinstance(value, str):
        return Template(value).substitute(names)
    if isinstance(value, list):
        return [_render(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, names) for k, v in value.items()}
    return value


def _result(session_id: str, spec: dict, text: str, is_error: bool) -> dict:
    return {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "session_id": session_id,
        "total_cost_usd": spec.get("cost_usd", 0.0),
        "num_turns": spec.get("num_turns", 1),
        "permission_denials": [],
        "result": text,
    }


def run(script: dict, session_id: str, invocation: int, prompt: str) -> int:
    entries = script.get("invocations") or [{}]
    spec = entries[min(invocation, len(entries) - 1)]
    names = {
        "session_id": session_id,
        "invocation": str(invocation),
        "cwd": os.getcwd(),
        "worker_id": os.environ.get("AGENTCTL_WORKER_ID", ""),
    }
    _emit({"type": "system", "subtype": "init", "session_id": session_id,
           "model": script.get("model", "fake"), "tools": [], "cwd": os.getcwd()})

    try:
        for step in spec.get("steps", []):
            if "capture" in step:
                cap = step["capture"]
                match = re.search(cap["regex"], prompt)
                if not match:
                    raise ValueError(f"capture {cap['name']!r}: regex not found in prompt")
                names[cap["name"]] = match.group(1) if match.groups() else match.group(0)
            elif "write" in step:
                w = step["write"]
                path = Path(_render(w["path"], names))
                path.parent.mkdir(parents=True, exist_ok=True)
                if "json" in w:
                    path.write_text(json.dumps(_render(w["json"], names), indent=2) + "\n")
                else:
                    path.write_text(_render(w.get("content", ""), names))
            elif "text" in step:
                _emit({"type": "assistant", "session_id": session_id,
                       "message": {"content": [{"type": "text", "text": _render(step["text"], names)}]}})
            elif "sleep" in step:
                time.sleep(float(step["sleep"]))
            elif "heartbeat" in step:
                hb = step["heartbeat"]
                for i in range(int(hb.get("count", 1))):
                    _emit({"type": "assistant", "session_id": session_id,
                           "message": {"content": [{"type": "text", "text": f"heartbeat {i}"}]}})
                    time.sleep(float(hb.get("interval", 0.1)))
            else:
                raise ValueError(f"unknown step {sorted(step)}")
    except (ValueError, KeyError, OSError) as exc:
        _emit(_result(session_id, spec, f"fake worker error: {exc}", True))
        return 3

    is_error = bool(spec.get("is_error", False))
    _emit(_result(session_id, spec, _render(spec.get("result", "done"), names), is_error))
    return int(spec.get("exit_code", 0))


def main() -> int:
    parser = argparse.ArgumentParser(prog="fake_worker")
    parser.add_argument("--script", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--invocation", type=int, default=0)
    args = parser.parse_args()
    script = json.loads(Path(args.script).read_text())
    return run(script, args.session_id, args.invocation, sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
