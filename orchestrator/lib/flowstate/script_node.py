"""Script nodes: run the node's script with FLOWSTATE_VAR_* in its working_dir."""

from pathlib import Path

from .model import Flow, Node
from .outputs import prebound, resolve_paths
from .procs import flow_env, run_captured, script_argv, tail
from .runtime import node_vars
from .state import RunStore
from .templating import render


def run(flow: Flow, node: Node, state: dict, store: RunStore, attempt_index: int) -> tuple[dict, dict]:
    paths = resolve_paths(flow, node, node_vars(state, node.id), store.dir)
    variables = node_vars(state, node.id, prebound(flow, node, paths))
    cwd = store.artefacts
    if node.working_dir:
        cwd = Path(render(node.working_dir, variables, flow.dir, "working_dir"))
        if not cwd.is_absolute():
            cwd = store.artefacts / cwd
    cwd.mkdir(parents=True, exist_ok=True)
    env = flow_env(variables, {"FLOWSTATE_RUN_ID": state["run_id"], "FLOWSTATE_RUN_DIR": str(store.dir),
                               "FLOWSTATE_NODE": node.id})
    timeout = node.timeout or state["config"]["script_timeout_s"]
    result = run_captured(script_argv(node.script), cwd.resolve(), env, timeout,
                          store.logs / node.id / f"attempt-{attempt_index}", "script")
    result["stderr_tail"] = tail(result["stderr"], 20)
    return result, paths
