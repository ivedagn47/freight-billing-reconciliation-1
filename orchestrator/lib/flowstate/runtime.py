"""Helpers shared by the engine and the node runners: situations, variables, retry feedback."""

from pathlib import Path

from .errors import FlowstateError
from .loader import load_flow
from .model import Flow, Node
from .scope import TOP, Scope
from .state import now_iso

RETRYABLE = {"validation_failed", "gate_failed", "worker_failed", "script_failed", "node_interrupted",
             "reducer_failed", "reducer_output_invalid", "reducer_interrupted"}
RESPAWNABLE = {"validation_failed", "gate_failed", "worker_failed", "worker_stalled", "worker_timeout"}
MAX_REPORTED_ERRORS = 10

OPTIONS = {
    "validation_failed": ["retry", "respawn", "abort"],
    "gate_failed": ["retry", "respawn", "abort"],
    "worker_failed": ["retry", "respawn", "abort"],
    "worker_stalled": ["advance --stall-after <seconds>", "respawn", "abort"],
    "worker_timeout": ["advance", "respawn", "abort"],
    "worker_running": ["advance"],
    "script_running": ["advance"],
    "branches_running": ["advance"],
    "script_failed": ["retry", "abort"],
    "node_interrupted": ["retry", "abort"],
    "render_failed": ["abort"],
    "condition_error": ["abort"],
    "no_route": ["abort"],
    "ambiguous_route": ["abort"],
    "fanout_invalid": ["abort"],
    "reducer_failed": ["retry", "abort"],
    "reducer_output_invalid": ["retry", "abort"],
    "reducer_interrupted": ["retry", "abort"],
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


def node_vars(state: dict, node_id: str, extra: dict | None = None, scope: Scope = TOP) -> dict:
    return {**scope.variables(state), "_node_id": node_id, **(extra or {})}


def situation(state: dict, kind: str, node_id: str | None = None, scope: Scope = TOP, **details) -> dict:
    sit = {"situation": kind, "run_id": state["run_id"], "at": now_iso()}
    options = list(OPTIONS.get(kind, []))
    if scope.is_branch:
        br = scope.branch_state(state)
        sit.update(parallel_node=scope.parallel, branch_id=scope.branch, branch_index=br["index"])
        if "item" in br["context"]:
            sit["item"] = br["context"]["item"]
    if node_id:
        ns = scope.node(state, node_id)
        sit.update(node=node_id, node_kind=ns["kind"], attempt=len(ns.get("attempts") or []),
                   retries_used=ns.get("retries_used", 0))
        if "max_retries" in ns:  # the same budget retry/respawn enforce; never recompute it elsewhere
            sit.update(max_retries=ns["max_retries"],
                       retries_remaining=max(0, ns["max_retries"] - ns.get("retries_used", 0)))
        if ns["kind"] != "agent":
            options = [o for o in options if o != "respawn"]
    if isinstance(details.get("errors"), list):
        total = len(details["errors"])
        details["errors"] = details["errors"][:MAX_REPORTED_ERRORS]
        if total > MAX_REPORTED_ERRORS:
            details["errors_truncated"] = total - MAX_REPORTED_ERRORS
    sit.update({k: v for k, v in details.items() if v is not None})
    if scope.is_branch and options and options[0] in ("retry", "respawn"):
        options = [f"{o} --branch {scope.branch}" if o in ("retry", "respawn") else o for o in options]
    sit["options"] = options
    return sit


def run_level_situation(state: dict) -> dict | None:
    """Run-status situations and the pending top-level situation, if any."""
    if state["status"] == "aborted":
        return situation(state, "aborted", reason=(state.get("abort") or {}).get("reason"))
    if state["status"] == "completed":
        return situation(state, "completed", variables=public_vars(state))
    if state["status"] == "paused":
        pause = dict(state.get("pause") or {})
        paused_at = pause.pop("at", None)
        node, branch, parallel = pause.pop("node", None), pause.pop("branch", None), pause.pop("parallel", None)
        scope = Scope(parallel, branch) if branch else TOP
        return situation(state, "paused", node, scope, paused_at=paused_at, **pause)
    return state.get("situation")


def public_vars(state: dict) -> dict:
    return {k: v for k, v in state["variables"].items() if not k.startswith("_")}


def event_fields(sit: dict) -> dict:
    return {k: v for k, v in sit.items() if k not in ("run_id", "options", "at")}


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
