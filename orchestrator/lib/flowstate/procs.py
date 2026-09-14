"""Subprocess execution: synchronous (gates) and detached (script nodes, reducers)."""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from .errors import FlowstateError
from .paths import VENV_BIN
from .templating import format_value

RUNNER = Path(__file__).resolve().parent / "script_runner.py"


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


def launch_detached(argv: list[str], cwd: Path, env: dict[str, str], timeout_s: float | None,
                    log_dir: Path, stem: str) -> int:
    """Start script_runner in its own session; returns the runner pid (also its process group)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("exit_code", "result.json", "runner.pid"):
        (log_dir / f"{stem}.{suffix}").unlink(missing_ok=True)
    evidence_env = {k: v for k, v in env.items() if k.startswith("FLOWSTATE_")}
    (log_dir / f"{stem}.command.json").write_text(json.dumps(
        {"argv": argv, "cwd": str(cwd), "timeout_s": timeout_s, "env": evidence_env}, indent=2) + "\n")
    with open(log_dir / f"{stem}.runner.log", "ab") as runner_log:
        proc = subprocess.Popen([sys.executable, str(RUNNER), str(log_dir), stem], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=runner_log, stderr=runner_log,
                                start_new_session=True)
    (log_dir / f"{stem}.runner.pid").write_text(f"{proc.pid}\n")
    return proc.pid


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)  # reap our own finished children (no zombies)
        if reaped == pid:
            return False
        return True
    except ChildProcessError:
        pass  # launched by an earlier flowstate process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def detached_state(log_dir: Path, stem: str) -> dict:
    """finished (with result) | running | lost (runner gone without writing its exit marker)."""
    exit_marker = log_dir / f"{stem}.exit_code"
    if exit_marker.exists():
        return {"state": "finished", **json.loads((log_dir / f"{stem}.result.json").read_text())}
    try:
        pid = int((log_dir / f"{stem}.runner.pid").read_text().strip())
    except (FileNotFoundError, ValueError):
        pid = None
    if pid_alive(pid) and not exit_marker.exists():
        return {"state": "running", "pid": pid}
    if exit_marker.exists():
        return detached_state(log_dir, stem)
    return {"state": "lost", "pid": pid}


def kill_detached(log_dir: Path, stem: str) -> bool:
    try:
        pid = int((log_dir / f"{stem}.runner.pid").read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def tail(path: str | Path, lines: int = 20) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return ""
