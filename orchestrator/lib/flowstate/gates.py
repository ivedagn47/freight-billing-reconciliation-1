"""Edge gates: deterministic executables; exit 0 passes, anything else (or a timeout) fails.

Evidence per evaluation: logs/gates/<source>--<target>/<label>/NN-<gate>.{stdout,stderr}.log
plus .result.json. Gates run in order and stop at the first failure.
"""

from pathlib import Path

from .model import Edge, Flow
from .procs import flow_env, run_captured, script_argv, tail

GATE_TIMEOUT_S = 120


def run(flow: Flow, edge: Edge, variables: dict, run_dir: Path, logs_dir: Path, label: str) -> dict:
    results = []
    for index, gate in enumerate(edge.gates):
        env = flow_env(variables, {
            "FLOWSTATE_RUN_ID": str(variables["_run_id"]),
            "FLOWSTATE_RUN_DIR": str(run_dir),
            "FLOWSTATE_EDGE": edge.id,
            "FLOWSTATE_FROM_NODE": edge.source,
            "FLOWSTATE_TO_NODE": edge.target,
        })
        res = run_captured(script_argv(gate), Path(variables["_run_artefact_dir"]), env, GATE_TIMEOUT_S,
                           logs_dir / "gates" / f"{edge.source}--{edge.target}" / label,
                           f"{index:02d}-{gate.stem}")
        res["gate"] = str(gate.relative_to(flow.dir))
        res["passed"] = res["exit_code"] == 0 and not res["timed_out"]
        res["stderr_tail"] = tail(res["stderr"], 10)
        results.append(res)
        if not res["passed"]:
            break
    return {"passed": all(r["passed"] for r in results), "results": results}
