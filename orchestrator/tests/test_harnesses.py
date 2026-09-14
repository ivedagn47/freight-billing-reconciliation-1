import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl import lifecycle
from agentctl.errors import AgentctlError
from agentctl.harnesses import get_harness
from agentctl.harnesses.base import TOOL_ALLOWLIST, InvocationSpec, validate_tools
from agentctl.transcript import summarize

from conftest import LIB


def _spec(index=0, **kw):
    base = dict(worker_id="w", session_id="11111111-2222-3333-4444-555555555555",
                cwd=Path("/tmp"), index=index, harness_opts={"bin": "/usr/bin/true"})
    base.update(kw)
    return InvocationSpec(**base)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_claude_spawn_command_enforces_isolation():
    argv = get_harness("claude").build_command(
        _spec(model="sonnet", permission_mode="auto", add_dirs=["/data"], max_budget_usd=0.5))
    assert argv[0] == "/usr/bin/true"
    assert argv[1:5] == ["-p", "--verbose", "--output-format", "stream-json"]
    assert _flag(argv, "--tools") == ",".join(TOOL_ALLOWLIST)
    assert _flag(argv, "--allowedTools") == ",".join(TOOL_ALLOWLIST)
    assert set(_flag(argv, "--disallowedTools").split(",")) >= {"Bash", "WebFetch", "WebSearch"}
    assert _flag(argv, "--setting-sources") == ""
    for flag in ("--disable-slash-commands", "--safe-mode", "--strict-mcp-config"):
        assert flag in argv
    assert _flag(argv, "--permission-prompts") == "none"
    assert _flag(argv, "--session-id") == _spec().session_id and "--resume" not in argv
    assert _flag(argv, "--model") == "sonnet"
    assert _flag(argv, "--permission-mode") == "auto"
    assert _flag(argv, "--add-dir") == "/data"
    assert _flag(argv, "--max-budget-usd") == "0.5"
    assert "--no-session-persistence" not in argv  # would break resume


def test_claude_send_command_resumes_same_session():
    argv = get_harness("claude").build_command(_spec(index=1))
    assert _flag(argv, "--resume") == _spec().session_id
    assert "--session-id" not in argv


def test_claude_rejects_bypass_permissions():
    with pytest.raises(AgentctlError):
        get_harness("claude").validate(_spec(permission_mode="bypassPermissions"))


def test_tool_policy_rejects_shell_and_web():
    assert validate_tools(["Read", "Write", "Read"]) == ["Read", "Write"]
    for bad in (["Bash"], ["Read", "WebFetch"], ["WebSearch"], ["Agent"]):
        with pytest.raises(AgentctlError):
            validate_tools(bad)


def test_spawn_rejects_disallowed_tools_before_writing(registry, workdir):
    with pytest.raises(AgentctlError):
        lifecycle.spawn(registry, "w1", harness="claude", cwd=str(workdir), prompt="x",
                        tools=["Read", "Bash"], harness_opts={"bin": "/usr/bin/true"})
    assert not registry.root.exists()


def test_unknown_harness():
    with pytest.raises(AgentctlError):
        get_harness("gpt")


def _run_fake(tmp_path, script, prompt="", invocation=0):
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script))
    proc = subprocess.run(
        [sys.executable, "-m", "agentctl.harnesses.fake_worker", "--script", str(path),
         "--session-id", "sid-1", "--invocation", str(invocation)],
        input=prompt, capture_output=True, text=True, cwd=tmp_path, env={"PYTHONPATH": str(LIB)})
    transcript = tmp_path / "out.jsonl"
    transcript.write_text(proc.stdout)
    return proc, summarize(transcript)


def test_fake_worker_captures_from_prompt_and_writes(tmp_path):
    script = {"invocations": [{
        "steps": [{"capture": {"name": "out", "regex": "path: (\\S+)"}},
                  {"write": {"path": "${out}", "json": {"_session_id": "${session_id}"}}},
                  {"text": "ok"}],
        "cost_usd": 0.25, "num_turns": 4, "result": "wrote ${out}"}]}
    proc, s = _run_fake(tmp_path, script, prompt="Write to path: nested/o.json please")
    assert proc.returncode == 0
    assert json.loads((tmp_path / "nested" / "o.json").read_text()) == {"_session_id": "sid-1"}
    assert (s["cost_usd"], s["num_turns"], s["result_text"]) == (0.25, 4, "wrote nested/o.json")


def test_fake_worker_is_deterministic_and_last_invocation_repeats(tmp_path):
    script = {"invocations": [{"result": "first"}, {"result": "later ${invocation}"}]}
    runs = [_run_fake(tmp_path, script, invocation=i)[1]["result_text"] for i in (0, 1, 5, 5)]
    assert runs == ["first", "later 1", "later 5", "later 5"]


def test_fake_worker_failure_modes(tmp_path):
    proc, s = _run_fake(tmp_path, {"invocations": [{"steps": [
        {"capture": {"name": "x", "regex": "absent"}}]}]})
    assert proc.returncode == 3 and s["is_error"]
    proc, s = _run_fake(tmp_path, {"invocations": [{"exit_code": 2, "is_error": True}]})
    assert proc.returncode == 2 and s["is_error"] and s["subtype"] == "error_during_execution"
