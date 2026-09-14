"""flowstate: graph runtime CLI.

    flowstate validate [FLOW]
    flowstate init [FLOW] --var K=V ... [--harness claude|fake --fake-script NODE=PATH ...]
    flowstate advance RUN [--max-wait S] [--stall-after S]    -> exactly one situation
    flowstate retry RUN NODE [--branch ID] [--feedback TEXT | --feedback-file PATH]
    flowstate respawn RUN NODE [--branch ID] [--reason TEXT]
    flowstate pause RUN [--reason TEXT] | resume RUN | abort RUN --reason TEXT
    flowstate status RUN | events RUN [--tail N] [--type T ...]

FLOW is a .dot path, a flow directory, or a stem under factory/flows/ (default_graph from
factory/factory-prefs.yml if omitted). RUN is a run id under runs/ or a run directory.
Output is JSON. Situations (including failures such as validation_failed) exit 0; command
errors print {"error": ...} and exit 1; `validate` exits 1 when the flow has errors.
"""

import argparse
import json
from pathlib import Path

from agentctl.errors import AgentctlError

from . import engine
from .errors import FlowstateError


def _pairs(values: list[str] | None, flag: str) -> dict[str, str]:
    out = {}
    for pair in values or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise FlowstateError("invalid_argument", f"{flag} expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--runs-dir", default=argparse.SUPPRESS, help="runs root (default: <repo>/runs)")
    parser = argparse.ArgumentParser(prog="flowstate", parents=[common], description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", parents=[common], help="static validation of a flow")
    p.add_argument("flow", nargs="?")

    p = sub.add_parser("init", parents=[common], help="validate a flow and create a run")
    p.add_argument("flow", nargs="?")
    p.add_argument("--var", action="append", default=[], help="NAME=VALUE run input")
    p.add_argument("--run-id")
    p.add_argument("--harness", default="claude", choices=("claude", "fake"))
    p.add_argument("--harness-opt", action="append", help="KEY=VALUE passed to the agentctl harness")
    p.add_argument("--fake-script", action="append", help="NODE=PATH script for the fake harness")
    p.add_argument("--supervision", choices=("low", "medium", "high"))
    p.add_argument("--max-retries", type=int, help="default per-node retry budget")

    p = sub.add_parser("advance", parents=[common], help="run until a situation needs a decision")
    p.add_argument("run")
    p.add_argument("--max-wait", type=float,
                   help="return worker_running / script_running / branches_running after this many seconds")
    p.add_argument("--stall-after", type=float, help="override stall threshold for this call")

    p = sub.add_parser("retry", parents=[common], help="retry a failed node (same worker conversation)")
    p.add_argument("run")
    p.add_argument("node")
    p.add_argument("--branch", help="branch id, for a node inside a fork/dynamic_fanout")
    p.add_argument("--feedback")
    p.add_argument("--feedback-file")

    p = sub.add_parser("respawn", parents=[common], help="replace a node's worker with a new session")
    p.add_argument("run")
    p.add_argument("node")
    p.add_argument("--branch", help="branch id, for a node inside a fork/dynamic_fanout")
    p.add_argument("--reason")

    p = sub.add_parser("pause", parents=[common], help="stop advancing the run")
    p.add_argument("run")
    p.add_argument("--reason")

    p = sub.add_parser("resume", parents=[common], help="continue a paused run")
    p.add_argument("run")

    p = sub.add_parser("abort", parents=[common], help="end the run and stop its workers")
    p.add_argument("run")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("status", parents=[common], help="concise run summary")
    p.add_argument("run")

    p = sub.add_parser("events", parents=[common], help="append-only event history")
    p.add_argument("run")
    p.add_argument("--tail", type=int)
    p.add_argument("--type", action="append", dest="types")
    return parser


def run(args: argparse.Namespace):
    runs_dir = getattr(args, "runs_dir", None)
    cmd = args.command
    if cmd == "validate":
        return engine.validate_flow(args.flow)
    if cmd == "init":
        return engine.init_run(args.flow, args.var, run_id=args.run_id, runs_dir=runs_dir,
                               harness=args.harness, harness_opts=_pairs(args.harness_opt, "--harness-opt"),
                               fake_scripts=_pairs(args.fake_script, "--fake-script"),
                               supervision=args.supervision, max_retries_default=args.max_retries)
    if cmd == "advance":
        return engine.advance(args.run, runs_dir=runs_dir, max_wait_s=args.max_wait,
                              stall_after_s=args.stall_after)
    if cmd == "retry":
        if args.feedback is not None and args.feedback_file is not None:
            raise FlowstateError("invalid_argument", "give --feedback or --feedback-file, not both")
        feedback = Path(args.feedback_file).read_text() if args.feedback_file else args.feedback
        return engine.retry(args.run, args.node, feedback, runs_dir=runs_dir, branch=args.branch)
    if cmd == "respawn":
        return engine.respawn(args.run, args.node, args.reason, runs_dir=runs_dir, branch=args.branch)
    if cmd == "pause":
        return engine.pause(args.run, args.reason, runs_dir=runs_dir)
    if cmd == "resume":
        return engine.resume(args.run, runs_dir=runs_dir)
    if cmd == "abort":
        return engine.abort(args.run, args.reason, runs_dir=runs_dir)
    if cmd == "status":
        return engine.status(args.run, runs_dir=runs_dir)
    if cmd == "events":
        return engine.events(args.run, args.tail, args.types, runs_dir=runs_dir)
    raise FlowstateError("invalid_argument", f"unknown command {cmd!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = run(args)
    except FlowstateError as exc:
        print(json.dumps(exc.to_json(), indent=2, default=str))
        return 1
    except (AgentctlError, OSError) as exc:
        print(json.dumps({"error": {"code": type(exc).__name__, "message": str(exc)}}, indent=2))
        return 1
    print(json.dumps(out, indent=2, default=str))
    if args.command == "validate" and not out["ok"]:
        return 1
    return 0
