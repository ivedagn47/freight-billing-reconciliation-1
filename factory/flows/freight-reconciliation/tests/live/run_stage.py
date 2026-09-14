"""Run one Phase 6 isolation flow with real Claude workers (spends tokens).

    orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/run_stage.py rules [--carriers alpine,falcon,sagar]
    orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/run_stage.py adjudicate
    orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/run_stage.py memos

`rules` extracts the real contracts (Opus, two copies each) against the real shipment vocabulary, with a
fresh cache so the cache is written, not read. `adjudicate` and `memos` run Sonnet on the synthetic acme
packets from tests/stage_flows.py, so no real reconciliation answer is produced here.

A minimal scripted supervisor drives the run: it retries a failed gate or validation (the worker gets
Flowstate's own feedback) while the node's retry budget lasts, and stops on anything else. It is not the
graph-orchestrator skill; it exists to show each agent node passing its gates in isolation. Everything is
written under runs/_phase6-live/ (gitignored).
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stage_flows  # noqa: E402
from agentctl import lifecycle  # noqa: E402
from agentctl.registry import Registry  # noqa: E402
from flowstate import engine  # noqa: E402

LIVE_ROOT = stage_flows.REPO / "runs" / "_phase6-live"
RUNS = LIVE_ROOT / "runs"
WAITING = {"worker_running", "script_running", "branches_running"}
RETRYABLE = {"gate_failed", "validation_failed"}
SHOWN = ("situation", "node", "branch_id", "item", "gate", "retries_remaining", "stderr_tail", "errors", "message",
         "worker_state", "subtype", "exit_code")


def stage_inputs(stage: str, work: Path, carriers: str, cache_dir: str | None) -> tuple[str, list[str]]:
    if stage == "rules":
        return "stage-rules", [f"carriers_config={stage_flows.FLOW_DIR / 'config' / 'carriers.yml'}",
                               f"data_root={stage_flows.REPO}",
                               f"shipments_file={stage_flows.REPO / 'data' / 'shipments.json'}",
                               f"carrier_ids={carriers}",
                               f"rules_cache_dir={Path(cache_dir).resolve() if cache_dir else work / 'cache'}"]
    scenario = stage_flows.acme_priced(work / "inputs")
    if stage == "adjudicate":
        return "stage-adjudicate", [f"priced={scenario['priced']}", f"clauses_dir={scenario['clauses_dir']}"]
    return "stage-memos", [f"priced={scenario['priced']}", f"report={scenario['report']}",
                           f"clauses_dir={scenario['clauses_dir']}", f"carriers_config={scenario['carriers']}"]


def drive(run_id: str) -> tuple[dict, list[dict]]:
    interventions = []
    while True:
        sit = engine.advance(run_id, runs_dir=str(RUNS), max_wait_s=120)
        if sit["situation"] in WAITING:
            print(f"  ... {sit['situation']} {json.dumps(sit.get('branches') or {})}", flush=True)
            continue
        print(json.dumps({k: sit[k] for k in SHOWN if k in sit}, indent=1), flush=True)
        if sit["situation"] in RETRYABLE and sit.get("retries_remaining", 0) > 0:
            engine.retry(run_id, sit["node"], runs_dir=str(RUNS), branch=sit.get("branch_id"))
            interventions.append({"action": "retry", "node": sit["node"], "branch": sit.get("branch_id"),
                                  "situation": sit["situation"], "gate": sit.get("gate")})
            continue
        return sit, interventions


def workers(run_dir: Path) -> list[dict]:
    registry = Registry(run_dir / "workers")
    rows = []
    for worker_id in registry.worker_ids():
        status = lifecycle.status(registry, worker_id)
        meta = json.loads((run_dir / "workers" / worker_id / "meta.json").read_text())
        rows.append({"worker": worker_id, "model": meta.get("model"), "state": status["state"],
                     "invocations": status.get("invocations"), "cost_usd": status.get("cost_usd"),
                     "num_turns": status.get("num_turns")})
    return rows


def gate_results(run_dir: Path) -> list[dict]:
    out = []
    for result in sorted((run_dir / "logs").rglob("*.result.json")):
        if "/gates/" not in str(result):
            continue
        data = json.loads(result.read_text())
        stdout = Path(data.get("stdout", ""))
        out.append({"gate": result.name.replace(".result.json", ""), "where": str(result.parent.relative_to(run_dir)),
                    "exit_code": data.get("exit_code"),
                    "stdout": stdout.read_text().strip()[-300:] if stdout.is_file() else None})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("rules", "adjudicate", "memos"))
    parser.add_argument("--carriers", default="alpine,falcon,sagar")
    parser.add_argument("--run-id")
    parser.add_argument("--cache-dir", help="rules: reuse this rate-spec cache (default: a fresh one per run)")
    args = parser.parse_args()

    run_id = args.run_id or f"phase6-{args.stage}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    work = LIVE_ROOT / "work" / run_id
    stage, variables = stage_inputs(args.stage, work, args.carriers, args.cache_dir)
    dot = stage_flows.materialize(stage, work / "flow")
    created = engine.init_run(str(dot), variables, run_id=run_id, runs_dir=str(RUNS), harness="claude")
    print(f"run {created['run_id']} at {created['run_dir']}", flush=True)

    final, interventions = drive(run_id)
    run_dir = Path(created["run_dir"])
    summary = {"run_id": run_id, "outcome": final["situation"], "interventions": interventions,
               "workers": workers(run_dir), "gates": gate_results(run_dir)}
    costs = [w["cost_usd"] for w in summary["workers"] if w["cost_usd"] is not None]
    summary["total_cost_usd"] = round(sum(costs), 4)
    if final["situation"] == "completed":
        summary["variables"] = {k: v for k, v in final["variables"].items()
                                if k in ("rules_final", "rate_specs", "adjudications", "memos_index")}
    (work / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if final["situation"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
