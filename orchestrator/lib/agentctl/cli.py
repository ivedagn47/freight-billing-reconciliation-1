"""agentctl: worker lifecycle CLI.

Every command except `logs` prints JSON on stdout. Failures print {"error": ...}
and exit 1. `wait` exits 0 whatever the outcome; read its "outcome" field
(exited | failed | killed | lost | stalled | timeout).
"""

import argparse
import json
import sys
from pathlib import Path

from . import lifecycle
from .errors import AgentctlError
from .harnesses import HARNESS_NAMES
from .registry import Registry


def _split_csv(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [t for v in values for t in v.replace(",", " ").split() if t]


def _opts(pairs: list[str] | None) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise AgentctlError(f"--harness-opt expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def _text(inline: str | None, path: str | None, what: str) -> str:
    if (inline is None) == (path is None):
        raise AgentctlError(f"give exactly one of --{what} or --{what}-file")
    return inline if inline is not None else Path(path).read_text()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", default=argparse.SUPPRESS,
                        help="worker registry dir (default: $AGENTCTL_REGISTRY)")

    parser = argparse.ArgumentParser(prog="agentctl", parents=[common], description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("spawn", parents=[common], help="start a worker in a tmux session")
    p.add_argument("name")
    p.add_argument("--harness", choices=HARNESS_NAMES, required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--model")
    p.add_argument("--permission-mode")
    p.add_argument("--session-id")
    p.add_argument("--allowed-tools", nargs="+", help="subset of Read,Write,Edit,Glob,Grep")
    p.add_argument("--add-dir", nargs="+", default=[])
    p.add_argument("--max-budget-usd", type=float)
    p.add_argument("--stall-after", type=float, help="seconds without transcript growth")
    p.add_argument("--harness-opt", action="append", help="KEY=VALUE, e.g. script=PATH for fake")

    p = sub.add_parser("wait", parents=[common], help="block until finished, stalled or timeout")
    p.add_argument("name")
    p.add_argument("--timeout", type=float)
    p.add_argument("--stall-after", type=float)

    p = sub.add_parser("send", parents=[common], help="continue an idle worker's session")
    p.add_argument("name")
    p.add_argument("--message")
    p.add_argument("--message-file")

    p = sub.add_parser("kill", parents=[common], help="stop a worker")
    p.add_argument("name")

    p = sub.add_parser("status", parents=[common], help="show one worker")
    p.add_argument("name")
    p.add_argument("--stall-after", type=float)

    sub.add_parser("list", parents=[common], help="show all workers in the registry")

    p = sub.add_parser("logs", parents=[common], help="print a worker's transcript or stderr")
    p.add_argument("name")
    p.add_argument("--stream", choices=("text", "transcript", "stderr"), default="text")
    p.add_argument("--invocation", type=int)
    p.add_argument("--tail", type=int)
    return parser


def run(args: argparse.Namespace) -> object:
    reg = Registry.resolve(getattr(args, "registry", None))
    cmd = args.command
    if cmd == "spawn":
        return lifecycle.spawn(
            reg, args.name, harness=args.harness, cwd=args.cwd,
            prompt=_text(args.prompt, args.prompt_file, "prompt"),
            model=args.model, permission_mode=args.permission_mode,
            session_id=args.session_id, tools=_split_csv(args.allowed_tools),
            add_dirs=args.add_dir, max_budget_usd=args.max_budget_usd,
            stall_after_s=args.stall_after, harness_opts=_opts(args.harness_opt))
    if cmd == "wait":
        return lifecycle.wait(reg, args.name, args.timeout, args.stall_after)
    if cmd == "send":
        return lifecycle.send(reg, args.name, _text(args.message, args.message_file, "message"))
    if cmd == "kill":
        return lifecycle.kill(reg, args.name)
    if cmd == "status":
        return lifecycle.status(reg, args.name, args.stall_after)
    if cmd == "list":
        return lifecycle.list_workers(reg)
    if cmd == "logs":
        return lifecycle.logs(reg, args.name, args.stream, args.invocation, args.tail)
    raise AgentctlError(f"unknown command {cmd!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = run(args)
    except (AgentctlError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    if isinstance(out, str):
        print(out)
    else:
        print(json.dumps(out, indent=2))
    return 0
