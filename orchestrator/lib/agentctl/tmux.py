"""Thin wrapper over the tmux CLI. Workers run in detached sessions so they
outlive the process that spawned them and a human can `tmux attach` to watch."""

import shlex
import shutil
import subprocess
from pathlib import Path

from .errors import AgentctlError


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    exe = shutil.which("tmux")
    if not exe:
        raise AgentctlError("tmux not found on PATH")
    proc = subprocess.run([exe, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AgentctlError(f"tmux {args[0]} failed: {proc.stderr.strip()}")
    return proc


def new_session(name: str, cwd: Path, argv: list[str], env: dict[str, str]) -> None:
    args = ["new-session", "-d", "-s", name, "-c", str(cwd)]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args.append(shlex.join(argv))
    _tmux(*args)


def has_session(name: str) -> bool:
    # "=name" forces an exact match; plain -t does prefix matching.
    return _tmux("has-session", "-t", f"={name}", check=False).returncode == 0


def kill_session(name: str) -> bool:
    if not has_session(name):
        return False
    _tmux("kill-session", "-t", f"={name}", check=False)
    return True


def list_sessions(prefix: str) -> list[str]:
    proc = _tmux("list-sessions", "-F", "#{session_name}", check=False)
    if proc.returncode != 0:  # no server running means no sessions
        return []
    return [s for s in proc.stdout.splitlines() if s.startswith(prefix)]
