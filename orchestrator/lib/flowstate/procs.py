"""Subprocess execution shared by script nodes and gates."""

import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from .errors import FlowstateError
from .paths import VENV_BIN
from .templating import format_value


def script_argv(path: Path) -> list[str]:
    """Run via the #! interpreter when present, so kit scripts committed without the
    executable bit still work; otherwise the file must be executable."""
    try:
        first = path.read_bytes()[:512].split(b"\n", 1)[0]
    except OSError as exc:
        raise FlowstateError("script_unreadable", f"cannot read {path}: {exc}") from exc
    if first.startswith(b"#!"):
        parts = shlex.split(first[2:].decode(errors="replace").strip())
        if parts:
            return [*parts, str(path)]
    if os.access(path, os.X_OK):
        return [str(path)]
    raise FlowstateError("not_executable", f"{path} has no #! line and is not executable")


def flow_env(variables: dict, extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOWSTATE_")}
    if VENV_BIN.is_dir():
        env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    for name, value in variables.items():
        if value is not None:
            env[f"FLOWSTATE_VAR_{name}"] = format_value(value)
    env.update(extra)
    return env


def run_captured(argv: list[str], cwd: Path, env: dict, timeout_s: float | None,
                 log_dir: Path, stem: str) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / f"{stem}.stdout.log", log_dir / f"{stem}.stderr.log"
    started = time.monotonic()
    exit_code, timed_out = None, False
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            err.write(f"flowstate: failed to start {argv[0]}: {exc}\n".encode())
            exit_code = 127
        else:
            try:
                exit_code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    result = {"argv": argv, "cwd": str(cwd), "exit_code": exit_code, "timed_out": timed_out,
              "duration_s": round(time.monotonic() - started, 3),
              "stdout": str(out_path), "stderr": str(err_path)}
    (log_dir / f"{stem}.result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def tail(path: str | Path, lines: int = 20) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return ""
