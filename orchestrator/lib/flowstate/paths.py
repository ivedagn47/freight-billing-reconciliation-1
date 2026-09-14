import os
import shutil
from pathlib import Path

import yaml

from .errors import FlowstateError

LIB_DIR = Path(__file__).resolve().parents[1]
ORCH_ROOT = LIB_DIR.parent
REPO_ROOT = ORCH_ROOT.parent
FLOWS_DIR = REPO_ROOT / "factory" / "flows"
PREFS_EXAMPLE = REPO_ROOT / "factory" / "factory-prefs-example.yml"
VENV_BIN = ORCH_ROOT / ".venv" / "bin"

PREF_DEFAULTS = {
    "supervision": "low",       # low | medium | high
    "default_graph": None,
    "max_retries": 2,           # per node, shared by retry and respawn
    "stall_after_s": 600,       # agent transcript silence before worker_stalled
    "script_timeout_s": 600,
    "max_parallel_branches": 4,  # per fork/dynamic_fanout, unless the node sets max_parallel
    "max_fanout_items": 500,     # per dynamic_fanout, unless the node sets max_items
}
SUPERVISION_LEVELS = ("low", "medium", "high")


def runs_root(override: str | None = None) -> Path:
    return Path(override or os.environ.get("FLOWSTATE_RUNS_DIR") or REPO_ROOT / "runs").resolve()


def load_prefs() -> dict:
    """factory/factory-prefs.yml, created from the example on first use (per the example's own
    comment). FLOWSTATE_PREFS points elsewhere, e.g. for tests."""
    override = os.environ.get("FLOWSTATE_PREFS")
    path = Path(override) if override else REPO_ROOT / "factory" / "factory-prefs.yml"
    if not path.exists() and not override and PREFS_EXAMPLE.exists():
        shutil.copyfile(PREFS_EXAMPLE, path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise FlowstateError("invalid_prefs", f"{path} must be a mapping")
    prefs = {**PREF_DEFAULTS, **data}
    if prefs["supervision"] not in SUPERVISION_LEVELS:
        raise FlowstateError("invalid_prefs", f"supervision must be one of {SUPERVISION_LEVELS}")
    return prefs


def resolve_flow(ref: str | None, prefs: dict) -> tuple[Path, Path]:
    """Accepts a .dot path, a flow directory, or a stem under factory/flows/."""
    ref = ref or prefs.get("default_graph")
    if not ref:
        raise FlowstateError("no_flow", "no flow given and no default_graph in prefs")
    p = Path(ref).expanduser()
    if p.suffix == ".dot":
        dot = p
    elif p.is_dir():
        dot = p / f"{p.resolve().name}.dot"
    else:
        dot = FLOWS_DIR / ref / f"{ref}.dot"
    dot = dot.resolve()
    if not dot.is_file():
        raise FlowstateError("flow_not_found", f"DOT file not found: {dot}")
    return dot, dot.parent / f"{dot.stem}.flow.yml"


def resolve_run(ref: str, runs_dir: str | None = None) -> Path:
    p = Path(ref).expanduser()
    candidates = [p] if (p / "state.yaml").exists() else [runs_root(runs_dir) / ref]
    for c in candidates:
        if (c / "state.yaml").exists():
            return c.resolve()
    raise FlowstateError("run_not_found", f"no run {ref!r} (looked in {candidates[0]})")
