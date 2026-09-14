"""Agent nodes: render the prompt and drive the worker through agentctl (Phase 1)."""

from pathlib import Path

from agentctl import lifecycle
from agentctl.registry import Registry

from .errors import FlowstateError
from .model import Flow, Node
from .outputs import prebound, resolve_paths
from .runtime import node_vars, prompt_footer
from .state import RunStore
from .templating import render


def context(flow: Flow, node: Node, state: dict, store: RunStore, session_id: str) -> dict:
    """Everything needed to launch: output paths, variables, cwd, rendered prompt."""
    paths = resolve_paths(flow, node, node_vars(state, node.id), store.dir)
    variables = node_vars(state, node.id, {**prebound(flow, node, paths), "_session_id": session_id})
    cwd = store.artefacts
    if node.working_dir:
        cwd = Path(render(node.working_dir, variables, flow.dir, "working_dir"))
        if not cwd.is_absolute():
            cwd = store.artefacts / cwd
    cwd = cwd.resolve()
    prompt = render(node.prompt_template.read_text(), variables, flow.dir, "prompt_template")
    return {
        "paths": paths,
        "cwd": cwd,
        "prompt": prompt + prompt_footer(session_id, cwd),
        "add_dirs": [render(d, variables, flow.dir, "add_dirs") for d in node.add_dirs],
    }


def registry(store: RunStore) -> Registry:
    return Registry(store.workers)


def spawn(flow: Flow, node: Node, state: dict, store: RunStore, worker_id: str, session_id: str,
          ctx: dict) -> dict:
    cfg = state["config"]
    harness = node.harness or cfg["harness"]
    opts = dict(cfg.get("harness_opts") or {})
    if harness == "fake":
        script = (cfg.get("fake_scripts") or {}).get(node.id)
        if not script:
            raise FlowstateError("fake_script_missing", f"no fake script configured for node {node.id!r}")
        opts["script"] = script
    ctx["cwd"].mkdir(parents=True, exist_ok=True)
    return lifecycle.spawn(
        registry(store), worker_id, harness=harness, cwd=str(ctx["cwd"]), prompt=ctx["prompt"],
        model=node.model, permission_mode=node.permission_mode, session_id=session_id,
        add_dirs=ctx["add_dirs"], max_budget_usd=node.max_budget_usd,
        stall_after_s=node.stall_after or cfg["stall_after_s"], harness_opts=opts)


def worker_status(store: RunStore, worker_id: str, stall_after_s: float | None = None) -> dict:
    return lifecycle.status(registry(store), worker_id, stall_after_s)


def worker_exists(store: RunStore, worker_id: str) -> bool:
    return registry(store).exists(worker_id)


def send(store: RunStore, worker_id: str, message: str) -> dict:
    return lifecycle.send(registry(store), worker_id, message)


def kill(store: RunStore, worker_id: str) -> dict:
    return lifecycle.kill(registry(store), worker_id)
