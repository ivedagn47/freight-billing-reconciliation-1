from ..errors import AgentctlError
from .base import Harness

HARNESS_NAMES = ("claude", "fake")


def get_harness(name: str) -> Harness:
    if name == "claude":
        from .claude import ClaudeHarness
        return ClaudeHarness()
    if name == "fake":
        from .fake import FakeHarness
        return FakeHarness()
    raise AgentctlError(f"unknown harness {name!r}; choose from {list(HARNESS_NAMES)}")
