"""On-disk worker registry.

The registry lives inside a run directory (flowstate passes runs/<run_id>/workers),
so every worker's prompt, transcript, stderr and exit status is run evidence.

    <registry>/<worker_id>/
        meta.json            spawn spec: harness, cwd, model, session_id, tools, ...
        prompt.md            prompt as given at spawn
        transcript.jsonl     all invocations' stream-json events, appended in order
        stderr.log           all invocations' stderr, appended in order
        killed.json          present once `agentctl kill` has run
        invocations/NNN/     one per process run (000 = spawn, 001+ = send)
            input.md  command.json  transcript.jsonl  stderr.log
            runner.pid  child.pid  exit_code  result.json
"""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import AgentctlError

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REGISTRY_ENV = "AGENTCTL_REGISTRY"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


class Registry:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def resolve(cls, arg: str | None) -> "Registry":
        root = arg or os.environ.get(REGISTRY_ENV)
        if not root:
            raise AgentctlError(f"no registry: pass --registry DIR or set {REGISTRY_ENV}")
        return cls(root)

    @property
    def tmux_prefix(self) -> str:
        # Scoped per registry so two runs can both have a worker named "research".
        digest = hashlib.sha1(str(self.root).encode()).hexdigest()[:8]
        return f"agentctl-{digest}-"

    def tmux_name(self, worker_id: str) -> str:
        return self.tmux_prefix + worker_id.replace(".", "_")

    def worker_dir(self, worker_id: str) -> Path:
        if not ID_RE.match(worker_id):
            raise AgentctlError(f"invalid worker id {worker_id!r}: use letters, digits, '.', '_', '-'")
        return self.root / worker_id

    def exists(self, worker_id: str) -> bool:
        return (self.worker_dir(worker_id) / "meta.json").exists()

    def load_meta(self, worker_id: str) -> dict:
        meta = read_json(self.worker_dir(worker_id) / "meta.json")
        if meta is None:
            raise AgentctlError(f"unknown worker {worker_id!r} in registry {self.root}")
        return meta

    def save_meta(self, worker_id: str, meta: dict) -> None:
        write_json_atomic(self.worker_dir(worker_id) / "meta.json", meta)

    def worker_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "meta.json").exists())

    def invocation_dir(self, worker_id: str, index: int) -> Path:
        return self.worker_dir(worker_id) / "invocations" / f"{index:03d}"

    def invocation_count(self, worker_id: str) -> int:
        inv_root = self.worker_dir(worker_id) / "invocations"
        if not inv_root.is_dir():
            return 0
        return sum(1 for p in inv_root.iterdir() if p.is_dir() and p.name.isdigit())
