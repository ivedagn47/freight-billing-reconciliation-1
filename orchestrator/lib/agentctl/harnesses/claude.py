"""Claude Code in headless print mode (`claude -p --output-format stream-json`).

Isolation flags were chosen by experiment (orchestrator/tests/live/probe_isolation.py;
evidence and reasoning in IMPLEMENTATION_LOG.md, Phase 1):

  --tools <allowlist>        only these built-in tools exist in the session
  --allowedTools <allowlist> pre-approves them so a headless worker never prompts
  --disallowedTools          defence in depth for shell and web tools
  --setting-sources ""       no user/project/local settings, project CLAUDE.md or project skills
  --disable-slash-commands   removes bundled/plugin skills that remain listed without it
  --safe-mode                disables auto-memory, which otherwise points every worker at
                             the repo's shared memory directory (cross-run leakage)
  --strict-mcp-config        no MCP servers
  --permission-prompts none  anything that would prompt is denied instead of hanging
"""

import shutil

from ..errors import AgentctlError
from .base import Harness, InvocationSpec

DISALLOWED_TOOLS = ("Bash", "WebFetch", "WebSearch")
FORBIDDEN_PERMISSION_MODES = ("bypassPermissions",)


class ClaudeHarness(Harness):
    name = "claude"

    def validate(self, spec: InvocationSpec) -> None:
        if spec.permission_mode in FORBIDDEN_PERMISSION_MODES:
            raise AgentctlError(f"permission mode {spec.permission_mode!r} is not allowed for workers")
        if not (spec.harness_opts.get("bin") or shutil.which("claude")):
            raise AgentctlError("claude CLI not found on PATH (or pass --harness-opt bin=PATH)")

    def build_command(self, spec: InvocationSpec) -> list[str]:
        exe = spec.harness_opts.get("bin") or shutil.which("claude")
        tools = ",".join(spec.tools)
        argv = [
            exe, "-p", "--verbose", "--output-format", "stream-json",
            "--tools", tools,
            "--allowedTools", tools,
            "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            "--setting-sources", "",
            "--disable-slash-commands",
            "--safe-mode",
            "--strict-mcp-config",
            "--permission-prompts", "none",
        ]
        # Spawn pins the session id; later invocations continue that same conversation.
        argv += ["--session-id", spec.session_id] if spec.index == 0 else ["--resume", spec.session_id]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]
        for directory in spec.add_dirs:
            argv += ["--add-dir", directory]
        if spec.max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{spec.max_budget_usd:g}"]
        return argv
