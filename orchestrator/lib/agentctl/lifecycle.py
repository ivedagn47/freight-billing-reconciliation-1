"""Worker lifecycle: spawn, wait, send, kill, status, list, logs.

States:
    running   invocation in progress, transcript growing
    stalled   invocation in progress, transcript unchanged for stall_after_s
    exited    finished, exit 0 and the harness reported no error
    failed    finished with a non-zero exit or an error result
    killed    stopped by `agentctl kill`
    lost      no exit code and no tmux session: the runner died unexpectedly
"""

import os
import signal
import sys
import time
import uuid
from pathlib import Path

from . import tmux
from .errors import AgentctlError
from .harnesses import get_harness
from .harnesses.base import InvocationSpec, validate_tools
from .registry import Registry, now_iso, read_json, write_json_atomic, write_text_atomic
from .transcript import iter_events, summarize

LIB_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STALL_AFTER_S = 600.0
POLL_S = 0.2
KILL_WAIT_S = 8.0
TERMINAL = ("exited", "failed", "killed", "lost")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spec(meta: dict, index: int) -> InvocationSpec:
    return InvocationSpec(
        worker_id=meta["id"], session_id=meta["session_id"], cwd=Path(meta["cwd"]),
        index=index, model=meta.get("model"), permission_mode=meta.get("permission_mode"),
        tools=list(meta["tools"]), add_dirs=list(meta["add_dirs"]),
        max_budget_usd=meta.get("max_budget_usd"), harness_opts=dict(meta.get("harness_opts", {})))


def _launch(reg: Registry, meta: dict, index: int, input_text: str) -> None:
    harness = get_harness(meta["harness"])
    spec = _spec(meta, index)
    harness.validate(spec)
    argv = harness.build_command(spec)

    inv = reg.invocation_dir(meta["id"], index)
    inv.mkdir(parents=True, exist_ok=False)
    write_text_atomic(inv / "input.md", input_text)
    write_json_atomic(inv / "command.json", {
        "argv": argv,
        "cwd": meta["cwd"],
        "env": {"AGENTCTL_WORKER_ID": meta["id"], "AGENTCTL_SESSION_ID": meta["session_id"],
                "AGENTCTL_INVOCATION": str(index)},
        "started_at": now_iso(),
    })
    runner = [sys.executable, "-m", "agentctl.runner", str(inv)]
    tmux.new_session(reg.tmux_name(meta["id"]), Path(meta["cwd"]), runner,
                     {"PYTHONPATH": str(LIB_DIR)})


def spawn(reg: Registry, worker_id: str, *, harness: str, cwd: str, prompt: str,
          model: str | None = None, permission_mode: str | None = None,
          session_id: str | None = None, tools: list[str] | None = None,
          add_dirs: list[str] | None = None, max_budget_usd: float | None = None,
          stall_after_s: float | None = None, harness_opts: dict[str, str] | None = None) -> dict:
    worker_dir = reg.worker_dir(worker_id)
    if reg.exists(worker_id):
        raise AgentctlError(f"worker {worker_id!r} already exists in {reg.root}")
    cwd_path = Path(cwd).expanduser().resolve()
    if not cwd_path.is_dir():
        raise AgentctlError(f"cwd is not a directory: {cwd_path}")
    try:
        session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
    except ValueError as exc:
        raise AgentctlError(f"session id must be a UUID: {session_id!r}") from exc
    resolved_dirs = []
    for d in add_dirs or []:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            raise AgentctlError(f"--add-dir is not a directory: {p}")
        resolved_dirs.append(str(p))
    if max_budget_usd is not None and max_budget_usd <= 0:
        raise AgentctlError("--max-budget-usd must be positive")

    impl = get_harness(harness)
    meta = {
        "id": worker_id,
        "harness": harness,
        "cwd": str(cwd_path),
        "model": model,
        "permission_mode": permission_mode,
        "session_id": session_id,
        "tools": validate_tools(tools) if tools is not None else validate_tools(
            list(InvocationSpec(worker_id, session_id, cwd_path, 0).tools)),
        "add_dirs": resolved_dirs,
        "max_budget_usd": max_budget_usd,
        "stall_after_s": stall_after_s or DEFAULT_STALL_AFTER_S,
        "harness_opts": impl.normalize_opts(harness_opts or {}),
        "tmux_session": reg.tmux_name(worker_id),
        "registry": str(reg.root),
        "created_at": now_iso(),
    }
    impl.validate(_spec(meta, 0))  # fail before anything is written
    worker_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(worker_dir / "prompt.md", prompt)
    reg.save_meta(worker_id, meta)
    _launch(reg, meta, 0, prompt)
    return status(reg, worker_id)


def status(reg: Registry, worker_id: str, stall_after_s: float | None = None) -> dict:
    meta = reg.load_meta(worker_id)
    worker_dir = reg.worker_dir(worker_id)
    count = reg.invocation_count(worker_id)
    if count == 0:
        raise AgentctlError(f"worker {worker_id!r} has no invocations")
    last = count - 1
    inv = reg.invocation_dir(worker_id, last)

    # Check the session before the exit code: a runner writes exit_code before it
    # exits, so this order can never report a cleanly finished worker as lost.
    alive = tmux.has_session(meta["tmux_session"])
    exit_code = _read_int(inv / "exit_code")
    killed = read_json(worker_dir / "killed.json")
    killed_now = bool(killed and killed.get("invocation") == last)

    transcript = inv / "transcript.jsonl"
    activity_ref = transcript if transcript.exists() else inv / "command.json"
    idle_s = max(0.0, time.time() - activity_ref.stat().st_mtime)
    stall_after = stall_after_s or meta.get("stall_after_s") or DEFAULT_STALL_AFTER_S

    invocations = []
    for i in range(count):
        inv_i = reg.invocation_dir(worker_id, i)
        summary = read_json(inv_i / "result.json") or summarize(inv_i / "transcript.jsonl")
        invocations.append({"index": i, "exit_code": _read_int(inv_i / "exit_code"), **summary})
    current = invocations[-1]

    if exit_code is not None:
        if killed_now:
            state = "killed"
        elif exit_code == 0 and not current.get("is_error"):
            state = "exited"
        else:
            state = "failed"
    elif killed_now and not alive:
        state = "killed"
    elif not alive:
        state = "lost"
    elif idle_s > stall_after:
        state = "stalled"
    else:
        state = "running"

    costs = [i["cost_usd"] for i in invocations if i.get("cost_usd") is not None]
    turns = [i["num_turns"] for i in invocations if i.get("num_turns") is not None]
    return {
        "id": worker_id,
        "state": state,
        "harness": meta["harness"],
        "model": meta.get("model"),
        "session_id": meta["session_id"],
        "tmux_session": meta["tmux_session"],
        "tmux_alive": alive,
        "cwd": meta["cwd"],
        "invocations": count,
        "exit_code": exit_code,
        "idle_s": round(idle_s, 2),
        "stall_after_s": stall_after,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "num_turns": sum(turns) if turns else None,
        "last_result": {
            "subtype": current.get("subtype"),
            "is_error": current.get("is_error"),
            "permission_denials": current.get("permission_denials"),
            "text": current.get("result_text"),
        },
        "per_invocation": [
            {k: i.get(k) for k in ("index", "exit_code", "subtype", "is_error", "cost_usd",
                                   "num_turns", "session_id")}
            for i in invocations
        ],
        "paths": {
            "dir": str(worker_dir),
            "transcript": str(worker_dir / "transcript.jsonl"),
            "stderr": str(worker_dir / "stderr.log"),
            "invocation_dir": str(inv),
        },
    }


def wait(reg: Registry, worker_id: str, timeout_s: float | None = None,
         stall_after_s: float | None = None) -> dict:
    start = time.monotonic()
    while True:
        st = status(reg, worker_id, stall_after_s)
        if st["state"] in TERMINAL or st["state"] == "stalled":
            return {"outcome": st["state"], **st}
        if timeout_s is not None and time.monotonic() - start >= timeout_s:
            return {"outcome": "timeout", **st}
        time.sleep(POLL_S)


def _await_session_gone(name: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not tmux.has_session(name):
            return True
        time.sleep(POLL_S)
    return not tmux.has_session(name)


def send(reg: Registry, worker_id: str, message: str) -> dict:
    meta = reg.load_meta(worker_id)
    st = status(reg, worker_id)
    if st["state"] in ("running", "stalled"):
        raise AgentctlError(f"worker {worker_id!r} is {st['state']}; wait for it or kill it first")
    if not _await_session_gone(meta["tmux_session"], 5.0):
        tmux.kill_session(meta["tmux_session"])  # runner already wrote exit_code
    _launch(reg, meta, reg.invocation_count(worker_id), message)
    return status(reg, worker_id)


def kill(reg: Registry, worker_id: str) -> dict:
    meta = reg.load_meta(worker_id)
    st = status(reg, worker_id)
    if st["state"] in ("exited", "failed", "killed"):
        return {"note": f"worker already {st['state']}; nothing to kill", **st}

    last = reg.invocation_count(worker_id) - 1
    inv = reg.invocation_dir(worker_id, last)
    write_json_atomic(reg.worker_dir(worker_id) / "killed.json",
                      {"invocation": last, "at": now_iso(), "previous_state": st["state"]})
    tmux.kill_session(meta["tmux_session"])  # runner gets SIGHUP and stops its child group

    child = _read_int(inv / "child.pid")
    runner = _read_int(inv / "runner.pid")
    deadline = time.monotonic() + KILL_WAIT_S
    while time.monotonic() < deadline and (
            _pid_alive(child) or (_pid_alive(runner) and not (inv / "exit_code").exists())):
        time.sleep(POLL_S)
    if _pid_alive(child):
        try:
            os.killpg(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not (inv / "exit_code").exists():  # runner itself died before recording
        write_json_atomic(inv / "result.json", summarize(inv / "transcript.jsonl"))
        write_text_atomic(inv / "exit_code", f"{-signal.SIGKILL}\n")
    return status(reg, worker_id)


def list_workers(reg: Registry) -> list[dict]:
    rows = []
    for worker_id in reg.worker_ids():
        st = status(reg, worker_id)
        rows.append({k: st[k] for k in ("id", "state", "harness", "model", "invocations",
                                        "exit_code", "cost_usd", "num_turns", "idle_s")})
    return rows


def logs(reg: Registry, worker_id: str, stream: str = "text",
         invocation: int | None = None, tail: int | None = None) -> str:
    reg.load_meta(worker_id)
    base = reg.worker_dir(worker_id) if invocation is None else reg.invocation_dir(worker_id, invocation)
    if stream == "stderr":
        path = base / "stderr.log"
        lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    elif stream == "transcript":
        path = base / "transcript.jsonl"
        lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    elif stream == "text":
        lines = [line for event in iter_events(base / "transcript.jsonl") for line in _render(event)]
    else:
        raise AgentctlError(f"unknown log stream {stream!r}")
    if tail:
        lines = lines[-tail:]
    return "\n".join(lines)


def _render(event: dict) -> list[str]:
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return [f"[init] session={event.get('session_id')} model={event.get('model')} "
                f"tools={event.get('tools')}"]
    if kind in ("assistant", "user"):
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                out.append(f"[{kind}] {block.get('text', '')}")
            elif block.get("type") == "tool_use":
                out.append(f"[tool_use] {block.get('name')} {str(block.get('input'))[:200]}")
            elif block.get("type") == "tool_result":
                out.append(f"[tool_result] {str(block.get('content'))[:200]}")
        return out
    if kind == "result":
        return [f"[result] {event.get('subtype')} is_error={event.get('is_error')} "
                f"turns={event.get('num_turns')} cost_usd={event.get('total_cost_usd')}"]
    return []
