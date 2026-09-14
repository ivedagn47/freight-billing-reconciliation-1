"""Script nodes: launch the node's script detached (script_runner.py), with FLOWSTATE_VAR_*."""

from pathlib import Path

from .model import Flow, Node
from .outputs import prebound, resolve_paths
from .procs import flow_env, launch_detached, script_argv
from .runtime import node_vars
from .scope import TOP, Scope
from .state import RunStore
from .templating import render

STEM = "script"


def launch(flow: Flow, node: Node, state: dict, store: RunStore, log_dir: Path, scope: Scope = TOP) -> int:
    paths = resolve_paths(flow, node, node_vars(state, node.id, scope=scope), store.dir)
    variables = node_vars(state, node.id, prebound(flow, node, paths), scope)
    base = Path(variables["_run_artefact_dir"])
    cwd = base
    if node.working_dir:
        cwd = Path(render(node.working_dir, variables, flow.dir, "working_dir"))
        if not cwd.is_absolute():
            cwd = base / cwd
    cwd.mkdir(parents=True, exist_ok=True)
    extra = {"FLOWSTATE_RUN_ID": state["run_id"], "FLOWSTATE_RUN_DIR": str(store.dir), "FLOWSTATE_NODE": node.id}
    if scope.is_branch:
        extra.update(FLOWSTATE_PARALLEL_NODE=scope.parallel, FLOWSTATE_BRANCH_ID=scope.branch)
    timeout = node.timeout or state["config"]["script_timeout_s"]
    return launch_detached(script_argv(node.script), cwd.resolve(), flow_env(variables, extra), timeout,
                           log_dir, STEM)
