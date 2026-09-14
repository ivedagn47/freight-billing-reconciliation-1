"""Runs one worker invocation inside its tmux session.

    python -m agentctl.runner <invocation_dir>

Feeds input.md on stdin to the harness command from command.json, tees stdout
(stream-json) into the invocation and worker transcripts, tees stderr likewise,
then writes result.json followed by exit_code. exit_code is written last: its
presence is the completion marker every other command relies on.
"""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .registry import read_json, write_json_atomic, write_text_atomic
from .transcript import summarize

# Variables that tie a process to the Claude Code session that launched agentctl
# (messaging socket, bridge/session ids, nesting markers). A worker inheriting them
# could attach to the orchestrator's session, so they are removed. Provider and
# config-location variables are kept so authentication still works.
KEEP_CLAUDE_VARS = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN"}
KILL_GRACE_S = 5.0


def worker_env(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items()
           if not (k.startswith("CLAUDE") and k not in KEEP_CLAUDE_VARS)}
    env.update(extra)
    return env


def _pump(src, *sinks) -> None:
    for chunk in iter(lambda: src.readline(), b""):
        for sink in sinks:
            sink.write(chunk)
            sink.flush()


def _terminate_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    timer = threading.Timer(KILL_GRACE_S, lambda: _hard_kill(pid))
    timer.daemon = True
    timer.start()


def _hard_kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run(inv_dir: Path) -> int:
    worker_dir = inv_dir.parent.parent
    command = read_json(inv_dir / "command.json")
    write_text_atomic(inv_dir / "runner.pid", f"{os.getpid()}\n")

    with open(inv_dir / "input.md", "rb") as stdin, \
            open(inv_dir / "transcript.jsonl", "ab") as inv_out, \
            open(worker_dir / "transcript.jsonl", "ab") as all_out, \
            open(inv_dir / "stderr.log", "ab") as inv_err, \
            open(worker_dir / "stderr.log", "ab") as all_err:
        try:
            child = subprocess.Popen(
                command["argv"], cwd=command["cwd"],
                env=worker_env(dict(os.environ), command.get("env", {})),
                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)  # own process group, so kill reaches grandchildren
        except OSError as exc:
            msg = f"agentctl runner: failed to start {command['argv'][0]}: {exc}\n".encode()
            inv_err.write(msg)
            all_err.write(msg)
            rc = 127
        else:
            write_text_atomic(inv_dir / "child.pid", f"{child.pid}\n")
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: _terminate_group(child.pid))
            err_thread = threading.Thread(target=_pump, args=(child.stderr, inv_err, all_err),
                                          daemon=True)
            err_thread.start()
            pane = sys.stdout.buffer
            _pump(child.stdout, inv_out, all_out, pane)
            rc = child.wait()
            err_thread.join(timeout=5)

    write_json_atomic(inv_dir / "result.json", summarize(inv_dir / "transcript.jsonl"))
    write_text_atomic(inv_dir / "exit_code", f"{rc}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]).resolve()))
