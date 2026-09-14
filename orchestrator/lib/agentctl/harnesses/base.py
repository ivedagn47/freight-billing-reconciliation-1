from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AgentctlError

# Worker tool policy: file tools only. No shell, no web, no subagents.
TOOL_ALLOWLIST = ("Read", "Write", "Edit", "Glob", "Grep")


@dataclass
class InvocationSpec:
    worker_id: str
    session_id: str
    cwd: Path
    index: int  # 0 = spawn; >= 1 = a `send` continuing the same session
    model: str | None = None
    permission_mode: str | None = None
    tools: list[str] = field(default_factory=lambda: list(TOOL_ALLOWLIST))
    add_dirs: list[str] = field(default_factory=list)
    max_budget_usd: float | None = None
    harness_opts: dict[str, str] = field(default_factory=dict)


def validate_tools(tools: list[str]) -> list[str]:
    outside = [t for t in tools if t not in TOOL_ALLOWLIST]
    if outside:
        raise AgentctlError(
            f"tools {outside} are outside the worker allowlist {list(TOOL_ALLOWLIST)}")
    return list(dict.fromkeys(tools))


class Harness:
    name = "base"

    def normalize_opts(self, opts: dict[str, str]) -> dict[str, str]:
        return dict(opts)

    def validate(self, spec: InvocationSpec) -> None:
        pass

    def build_command(self, spec: InvocationSpec) -> list[str]:
        raise NotImplementedError
