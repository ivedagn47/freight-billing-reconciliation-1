"""Run directory, authoritative state.yaml and append-only events.jsonl.

    runs/<run_id>/
        state.yaml      authoritative; only mutated inside RunStore.transaction()
        events.jsonl    append-only evidence, written after the state change it describes
        artefacts/      node outputs ({_run_artefact_dir})
        workers/        agentctl registry: workers/<worker_id>/ (prompt, transcripts, exit codes)
        logs/           script stdout/stderr, gate evidence, snapshots of rejected outputs

Two locks: `.state.lock` serialises every read-modify-write of state.yaml (held briefly),
and `.advance.lock` is a non-blocking lease so only one `advance` drives a run at a time
while `status`, `pause` and `abort` stay usable.
"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .errors import FlowstateError


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunStore:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir).resolve()
        self.state_path = self.dir / "state.yaml"
        self.events_path = self.dir / "events.jsonl"
        self.artefacts = self.dir / "artefacts"
        self.workers = self.dir / "workers"
        self.logs = self.dir / "logs"

    def create_layout(self) -> None:
        if self.dir.exists():
            raise FlowstateError("run_exists", f"run directory already exists: {self.dir}")
        self.dir.mkdir(parents=True)
        for d in (self.artefacts, self.workers, self.logs):
            d.mkdir()

    @contextmanager
    def _flock(self, name: str, blocking: bool = True):
        fd = os.open(self.dir / name, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise FlowstateError("run_busy", "another `flowstate advance` is driving this run") from exc
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def read(self) -> dict:
        try:
            data = yaml.safe_load(self.state_path.read_text())
        except FileNotFoundError as exc:
            raise FlowstateError("run_not_found", f"no state.yaml in {self.dir}") from exc
        if not isinstance(data, dict):
            raise FlowstateError("corrupt_state", f"{self.state_path} is not a mapping")
        return data

    def _write(self, state: dict) -> None:
        state["revision"] = int(state.get("revision", 0)) + 1
        state["updated_at"] = now_iso()
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state.", suffix=".yaml")
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(state, fh, sort_keys=False, default_flow_style=False, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def write_initial(self, state: dict) -> None:
        with self._flock(".state.lock"):
            self._write(state)

    @contextmanager
    def transaction(self):
        """Read-modify-write under the state lock. Nothing is written if the body raises."""
        with self._flock(".state.lock"):
            state = self.read()
            yield state
            self._write(state)

    @contextmanager
    def advance_lease(self):
        with self._flock(".advance.lock", blocking=False):
            yield

    def event(self, kind: str, **data) -> dict:
        record = {"ts": now_iso(), "type": kind, **{k: v for k, v in data.items() if v is not None}}
        line = json.dumps(record, default=str) + "\n"
        with self._flock(".events.lock"):
            with open(self.events_path, "a") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        return record

    def events(self, tail: int | None = None, kinds: set[str] | None = None) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        for raw in self.events_path.read_text().splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if kinds is None or rec.get("type") in kinds:
                out.append(rec)
        return out[-tail:] if tail else out
