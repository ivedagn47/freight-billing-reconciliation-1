"""Helpers shared by the engine and the node runners: situations, variables, retry feedback."""

from pathlib import Path

from .errors import FlowstateError
from .loader import load_flow
from .model import Flow, Node
from .state import now_iso

RETRYABLE = {"validation_failed", "gate_failed", "worker_failed", "script_failed", "node_interrupted"}
RESPAWNABLE = {"validation_failed", "gate_failed", "worker_failed", "worker_stalled", "worker_timeout"}
MAX_REPORTED_ERRORS = 10

OPTIONS = {
    "validation_failed": ["retry", "respawn", "abort"],
    "gate_failed": ["retry", "respawn", "abort"],
    "worker_failed": ["retry", "respawn", "abort"],
    "worker_stalled": ["advance --stall-after <seconds>", "respawn", "abort"],
    "worker_timeout": ["advance", "respawn", "abort"],
    "worker_running": ["advance"],
    "script_failed": ["retry", "abort"],
    "node_interrupted": ["retry", "abort"],
    "render_failed": ["abort"],
    "condition_error": ["abort"],
    "no_route": ["abort"],
    "ambiguous_route": ["abort"],
    "retries_exhausted": ["abort"],
    "paused": ["resume", "abort"],
    "busy": ["advance (after the other advance finishes)"],
    "flow_changed": ["abort"],
    "aborted": [],
    "completed": [],
}


def load_run_flow(state: dict) -> Flow:
    flow = load_flow(Path(state["flow"]["dot"]), Path(state["flow"]["yml"]))
    if flow.digest != state["flow"]["digest"]:
        raise FlowstateError("flow_changed", "flow definition files changed since the run was created; "
                             "outputs would no longer be reproducible from the recorded definition",
                             {"recorded": state["flow"]["digest"], "current": flow.digest})
    return flow


def max_retries(node: Node, state: dict) -> int:
    return node.max_retries if node.max_retries is not None else int(state["config"]["max_retries"])


def node_vars(state: dict, node_id: str, extra: dict | None = None) -> dict:
    return {**state["variables"], "_node_id": node_id, **(extra or {})}


def situation(state: dict, kind: str, node_id: str | None = None, **details) -> dict:
    sit = {"situation": kind, "run_id": state["run_id"], "at": now_iso()}
    options = list(OPTIONS.get(kind, []))
    if node_id:
        ns = state["nodes"][node_id]
        sit.update(node=node_id, node_kind=ns["kind"], attempt=len(ns.get("attempts") or []),
                   retries_used=ns.get("retries_used", 0))
        if ns["kind"] != "agent":
            options = [o for o in options if o != "respawn"]
    if isinstance(details.get("errors"), list):
        total = len(details["errors"])
        details["errors"] = details["errors"][:MAX_REPORTED_ERRORS]
        if total > MAX_REPORTED_ERRORS:
            details["errors_truncated"] = total - MAX_REPORTED_ERRORS
    sit.update({k: v for k, v in details.items() if v is not None})
    sit["options"] = options
    return sit


def prompt_footer(session_id: str, cwd: Path) -> str:
    return (
        "\n\n---\n"
        "Flowstate runtime context (added by the runtime, not part of the task):\n"
        f"- Your session id is {session_id}. Wherever the task asks for your session id or a "
        f"\"_session_id\" value, use exactly this string.\n"
        f"- Your working directory is {cwd}.\n"
        "- Write output files at exactly the paths the task gives. Outputs are validated "
        "automatically; missing or invalid outputs are rejected.\n"
    )


def feedback_message(node_id: str, sit: dict, session_id: str, extra: str | None,
                     expected_outputs: dict[str, str] | None = None) -> str:
    lines = [f"Flowstate rejected the result of node `{node_id}` ({sit['situation']})."]
    if sit.get("errors"):
        lines += ["", "Problems found:"]
        for e in sit["errors"]:
            where = " ".join(x for x in (e.get("file"), e.get("path"), e.get("at")) if x)
            lines.append(f"- {where}: {e.get('error')}: {e.get('message', '')}".rstrip(": "))
    if sit["situation"] == "gate_failed":
        lines += ["", f"Check `{sit.get('gate')}` failed with exit code {sit.get('exit_code')}.",
                  sit.get("stderr_tail") or "(no stderr)"]
    if sit["situation"] == "worker_failed":
        lines += ["", f"Your previous run ended as {sit.get('worker_state')} "
                      f"({sit.get('subtype') or 'no result'}). Continue the original task."]
    if expected_outputs:
        lines += ["", "Required output files:"]
        lines += [f"- {name}: {path}" for name, path in sorted(expected_outputs.items())]
    lines += ["", "Fix the output files in place, at the same paths.",
              f"Every JSON output must keep \"_session_id\": \"{session_id}\"."]
    if extra:
        lines += ["", "Additional instructions from the orchestrator:", extra]
    return "\n".join(lines) + "\n"
