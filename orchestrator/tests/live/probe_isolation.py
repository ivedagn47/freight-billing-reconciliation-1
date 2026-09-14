"""Experimental check that worker isolation flags keep project CLAUDE.md and skills out.

Builds a fixture project inside the repo (so the repo CLAUDE.md is also an
ancestor), plants canary tokens in a CLAUDE.md and a project skill, then runs a
trivial haiku worker under several flag sets. Uses the runner's real env scrub.

    orchestrator/.venv/bin/python orchestrator/tests/live/probe_isolation.py [--configs B0,I4]

Evidence per config: the stream-json init event (tools, skills, slash commands,
plugins, MCP servers, memory paths, messaging socket) plus the model's own report
of canary tokens. Writes runs/_isolation-probe/report.json. Spends tokens.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "orchestrator" / "lib"))

from agentctl.harnesses.base import TOOL_ALLOWLIST  # noqa: E402
from agentctl.runner import worker_env  # noqa: E402
from agentctl.transcript import summarize  # noqa: E402

OUT = REPO / "runs" / "_isolation-probe"
FIXTURE = OUT / "project"
MEMORY_CANARY = "CANARY-MEMORY-7Q3X"
SKILL_CANARY = "CANARY-SKILL-K9P2"
ALLOW = ",".join(TOOL_ALLOWLIST)

CONFIGS = {
    "B0-baseline": ["--tools", ""],
    "I1-sources-empty": ["--tools", "", "--setting-sources", ""],
    "I2-sources-empty+no-slash": ["--tools", "", "--setting-sources", "", "--disable-slash-commands"],
    "I3-safe-mode": ["--tools", "", "--safe-mode"],
    "I4-candidate": ["--tools", ALLOW, "--setting-sources", "", "--disable-slash-commands",
                     "--strict-mcp-config", "--add-dir", str(REPO / "data")],
    "I5-candidate+adddir-repo": ["--tools", ALLOW, "--setting-sources", "", "--disable-slash-commands",
                                 "--strict-mcp-config", "--add-dir", str(REPO)],
    "I6-final": ["--tools", ALLOW, "--setting-sources", "", "--disable-slash-commands",
                 "--strict-mcp-config", "--safe-mode", "--add-dir", str(REPO)],
}
# Claude Code gives every process its own messaging socket; isolation only requires
# that a worker is not attached to the socket of the session that launched it.
PARENT_SOCKET = __import__("os").environ.get("CLAUDE_CODE_MESSAGING_SOCKET")

PROMPT = (
    "Do not use any tools. Answer in exactly two lines.\n"
    "Line 1: every token matching CANARY-[A-Z0-9-]+ that appears anywhere in your system prompt, "
    "instructions, memory/CLAUDE.md content, or list of available skills or commands; NONE if none.\n"
    "Line 2: YES if anything in your context mentions 'BlueFin', otherwise NO.\n"
)


def build_fixture() -> None:
    (FIXTURE / ".claude" / "skills" / "canary-probe").mkdir(parents=True, exist_ok=True)
    (FIXTURE / "CLAUDE.md").write_text(
        f"# Probe project memory\n\nThe secret memory canary token is {MEMORY_CANARY}. "
        "If asked about canary tokens, report it.\n")
    (FIXTURE / ".claude" / "skills" / "canary-probe" / "SKILL.md").write_text(
        f"---\nname: canary-probe\ndescription: Probe skill. Its canary token is {SKILL_CANARY}.\n"
        f"---\n\nReport the token {SKILL_CANARY} when asked about canary tokens.\n")


def probe(name: str, flags: list[str], model: str) -> dict:
    argv = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--model", model,
            "--permission-prompts", "none", "--no-session-persistence", *flags]
    proc = subprocess.run(argv, cwd=FIXTURE, env=worker_env(dict(__import__("os").environ), {}),
                          input=PROMPT, capture_output=True, text=True, timeout=300)
    transcript = OUT / f"{name}.jsonl"
    transcript.write_text(proc.stdout)
    (OUT / f"{name}.stderr.log").write_text(proc.stderr)
    s = summarize(transcript)
    init = s.get("init") or {}
    answer = (s.get("result_text") or "").strip()
    names = (init.get("skills") or []) + (init.get("slash_commands") or [])
    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    evidence = {
        "exit_code": proc.returncode,
        "completed": s["completed"],
        "tools": init.get("tools"),
        "skills_count": len(init.get("skills") or []),
        "slash_commands_count": len(init.get("slash_commands") or []),
        "canary_skill_listed": "canary-probe" in names,
        "plugins": init.get("plugins"),
        "mcp_servers": init.get("mcp_servers"),
        "memory_paths": init.get("memory_paths"),
        "messaging_socket_path": init.get("messaging_socket_path"),
        "answer": answer,
        "memory_canary_reported": MEMORY_CANARY in answer,
        "skill_canary_reported": SKILL_CANARY in answer,
        "bluefin_reported": bool(lines) and lines[-1].upper().startswith("YES"),
        "cost_usd": s.get("cost_usd"),
        "stderr": proc.stderr.strip()[:500],
    }
    evidence["isolated"] = (
        evidence["completed"]
        and not evidence["canary_skill_listed"]
        and evidence["skills_count"] == 0
        and not evidence["memory_canary_reported"]
        and not evidence["skill_canary_reported"]
        and not evidence["bluefin_reported"]
        and not evidence["memory_paths"]
        and (PARENT_SOCKET is None or evidence["messaging_socket_path"] != PARENT_SOCKET)
        and set(evidence["tools"] or []) <= set(TOOL_ALLOWLIST)
    )
    return {"config": name, "flags": flags, **evidence}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--model", default="haiku")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    build_fixture()
    rows = []
    for name in args.configs.split(","):
        row = probe(name, CONFIGS[name], args.model)
        rows.append(row)
        print(json.dumps({k: row[k] for k in (
            "config", "completed", "isolated", "tools", "skills_count", "slash_commands_count",
            "canary_skill_listed", "memory_canary_reported", "skill_canary_reported",
            "bluefin_reported", "memory_paths", "messaging_socket_path", "answer", "stderr")}))
    (OUT / "report.json").write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
