import json

from agentctl.runner import worker_env
from agentctl.transcript import summarize


def _write_lines(path, events, trailing=""):
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + trailing)


def test_summarize_reads_init_text_and_result(tmp_path):
    t = tmp_path / "t.jsonl"
    _write_lines(t, [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "m",
         "tools": ["Read"], "skills": [], "memory_paths": None},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s1",
         "total_cost_usd": 0.0123, "num_turns": 3, "permission_denials": [], "result": "done"},
    ], trailing='{"type": "assist')  # a line still being written
    s = summarize(t)
    assert s["completed"] and s["session_id"] == "s1" and s["model"] == "m"
    assert (s["cost_usd"], s["num_turns"], s["is_error"]) == (0.0123, 3, False)
    assert s["result_text"] == "done"
    assert s["init"]["tools"] == ["Read"]
    assert s["events"] == 3


def test_summarize_in_progress_and_missing(tmp_path):
    t = tmp_path / "t.jsonl"
    _write_lines(t, [{"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}])
    s = summarize(t)
    assert not s["completed"] and s["cost_usd"] is None and s["result_text"] == "working"
    assert summarize(tmp_path / "absent.jsonl")["events"] == 0


def test_worker_env_scrubs_orchestrator_session_vars():
    base = {
        "PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k",
        "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_MESSAGING_SOCKET": "/s",
        "CLAUDE_CODE_MESSAGING_TOKEN": "t", "CLAUDE_CODE_BRIDGE_SESSION_ID": "b", "CLAUDE_PID": "1",
        "CLAUDE_JOB_DIR": "/j", "CLAUDE_CONFIG_DIR": "/c", "CLAUDE_CODE_USE_BEDROCK": "1",
    }
    env = worker_env(base, {"AGENTCTL_WORKER_ID": "w"})
    for gone in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                 "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_PID",
                 "CLAUDE_JOB_DIR"):
        assert gone not in env
    for kept in ("PATH", "HOME", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK"):
        assert env[kept] == base[kept]
    assert env["AGENTCTL_WORKER_ID"] == "w"
