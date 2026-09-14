import sys
from pathlib import Path

from ..errors import AgentctlError
from .base import Harness, InvocationSpec


class FakeHarness(Harness):
    """Runs fake_worker with a JSON script: deterministic, free, same event shapes."""

    name = "fake"

    def normalize_opts(self, opts: dict[str, str]) -> dict[str, str]:
        opts = dict(opts)
        if "script" in opts:
            opts["script"] = str(Path(opts["script"]).expanduser().resolve())
        return opts

    def validate(self, spec: InvocationSpec) -> None:
        script = spec.harness_opts.get("script")
        if not script:
            raise AgentctlError("fake harness requires --harness-opt script=PATH")
        if not Path(script).is_file():
            raise AgentctlError(f"fake harness script not found: {script}")

    def build_command(self, spec: InvocationSpec) -> list[str]:
        return [sys.executable, "-m", "agentctl.harnesses.fake_worker",
                "--script", spec.harness_opts["script"],
                "--session-id", spec.session_id,
                "--invocation", str(spec.index)]
