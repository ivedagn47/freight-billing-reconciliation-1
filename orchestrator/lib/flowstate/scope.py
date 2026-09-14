"""Where a node's execution state lives in state.yaml.

The top level (Phase 2) keeps `nodes`, `variables`, `cursor` and `situation` at the root.
A branch of a fork/dynamic_fanout keeps the same shapes under
`parallel.<parallel_node>.branches.<branch_id>`, so node execution is written once
against a Scope instead of twice.
"""

from dataclasses import dataclass
from pathlib import Path


def new_node_state(kind: str) -> dict:
    return {"kind": kind, "status": "pending", "retries_used": 0, "attempts": []}


@dataclass(frozen=True)
class Scope:
    parallel: str | None = None
    branch: str | None = None

    @property
    def is_branch(self) -> bool:
        return self.branch is not None

    def branch_state(self, st: dict) -> dict:
        return st["parallel"][self.parallel]["branches"][self.branch]

    def nodes(self, st: dict) -> dict:
        return self.branch_state(st)["nodes"] if self.is_branch else st["nodes"]

    def node(self, st: dict, node_id: str) -> dict:
        return self.nodes(st)[node_id]

    def variables(self, st: dict) -> dict:
        """Effective read view: run variables, then branch context, then branch outputs."""
        if not self.is_branch:
            return st["variables"]
        br = self.branch_state(st)
        return {**st["variables"], **br["context"], **br["variables"]}

    def commit(self, st: dict, values: dict) -> None:
        (self.branch_state(st)["variables"] if self.is_branch else st["variables"]).update(values)

    def cursor(self, st: dict) -> str | None:
        return self.branch_state(st)["cursor"] if self.is_branch else st["cursor"]

    def set_cursor(self, st: dict, node_id: str | None) -> None:
        if self.is_branch:
            self.branch_state(st)["cursor"] = node_id
        else:
            st["cursor"] = node_id

    def situation(self, st: dict) -> dict | None:
        return self.branch_state(st).get("situation") if self.is_branch else st.get("situation")

    def set_situation(self, st: dict, sit: dict | None) -> None:
        if not self.is_branch:
            st["situation"] = sit
            return
        br = self.branch_state(st)
        br["situation"] = sit
        if br["status"] != "completed":
            br["status"] = "awaiting_decision" if sit else "running"

    def worker_id(self, node_id: str) -> str:
        return f"{node_id}.{self.branch}" if self.is_branch else node_id

    def log_dir(self, logs: Path, node_id: str) -> Path:
        return logs / "branches" / self.branch / node_id if self.is_branch else logs / node_id

    def labels(self) -> dict:
        return {"parallel": self.parallel, "branch": self.branch} if self.is_branch else {}


TOP = Scope()
