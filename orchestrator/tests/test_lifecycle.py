import json
import os
import signal
import time

import pytest

from agentctl import cli, lifecycle, tmux
from agentctl.errors import AgentctlError
from agentctl.registry import Registry

from conftest import requires_tmux

pytestmark = [requires_tmux, pytest.mark.tmux]


def _spawn(registry, workdir, script, name="w1", prompt="go", **kw):
    return lifecycle.spawn(registry, name, harness="fake", cwd=str(workdir), prompt=prompt,
                           harness_opts={"script": script}, **kw)


def _pid(path):
    return int(path.read_text())


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_spawn_wait_send_roundtrip(registry, workdir, fake_script):
    script = fake_script(
        {"steps": [{"capture": {"name": "out", "regex": "path: (\\S+)"}},
                   {"write": {"path": "${out}", "json": {"_session_id": "${session_id}"}}}],
         "cost_usd": 0.25, "num_turns": 2},
        {"steps": [{"write": {"path": "followup.txt", "content": "${invocation}"}}],
         "cost_usd": 0.5, "num_turns": 1})
    st = _spawn(registry, workdir, script, prompt="Write to path: out.json",
                session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert st["state"] == "running" and st["tmux_alive"]

    done = lifecycle.wait(registry, "w1", timeout_s=20)
    assert done["outcome"] == "exited" and done["exit_code"] == 0
    assert json.loads((workdir / "out.json").read_text())["_session_id"] == st["session_id"]

    lifecycle.send(registry, "w1", "follow up")
    done = lifecycle.wait(registry, "w1", timeout_s=20)
    assert done["outcome"] == "exited" and done["invocations"] == 2
    assert (workdir / "followup.txt").read_text() == "1"
    assert done["cost_usd"] == 0.75 and done["num_turns"] == 3
    assert {i["session_id"] for i in done["per_invocation"]} == {st["session_id"]}

    wdir = registry.worker_dir("w1")
    assert (wdir / "prompt.md").read_text() == "Write to path: out.json"
    assert (registry.invocation_dir("w1", 1) / "input.md").read_text() == "follow up"
    results = [json.loads(l) for l in (wdir / "transcript.jsonl").read_text().splitlines()]
    assert sum(1 for e in results if e["type"] == "result") == 2
    assert not tmux.has_session(registry.tmux_name("w1"))


def test_failed_workers(registry, workdir, fake_script):
    _spawn(registry, workdir, fake_script({"exit_code": 2}), name="nonzero")
    assert lifecycle.wait(registry, "nonzero", timeout_s=20)["outcome"] == "failed"
    _spawn(registry, workdir, fake_script({"is_error": True}), name="erresult")
    st = lifecycle.wait(registry, "erresult", timeout_s=20)
    assert st["outcome"] == "failed" and st["exit_code"] == 0


def test_stall_detection_then_kill(registry, workdir, fake_script):
    _spawn(registry, workdir, fake_script({"steps": [{"text": "start"}, {"sleep": 60}]}),
           stall_after_s=1.0)
    st = lifecycle.wait(registry, "w1", timeout_s=20)
    assert st["outcome"] == "stalled" and st["tmux_alive"]

    inv = registry.invocation_dir("w1", 0)
    child = _pid(inv / "child.pid")
    killed = lifecycle.kill(registry, "w1")
    assert killed["state"] == "killed" and killed["exit_code"] is not None
    assert not tmux.has_session(registry.tmux_name("w1"))
    assert not _alive(child)


def test_steady_output_is_not_stalled(registry, workdir, fake_script):
    _spawn(registry, workdir, fake_script({"steps": [{"heartbeat": {"count": 8, "interval": 0.3}}]}),
           stall_after_s=1.0)
    assert lifecycle.wait(registry, "w1", timeout_s=20)["outcome"] == "exited"


def test_wait_timeout_leaves_worker_running(registry, workdir, fake_script):
    _spawn(registry, workdir, fake_script({"steps": [{"sleep": 60}]}))
    st = lifecycle.wait(registry, "w1", timeout_s=0.5)
    assert st["outcome"] == "timeout" and st["state"] == "running"
    with pytest.raises(AgentctlError, match="running"):
        lifecycle.send(registry, "w1", "too early")
    assert lifecycle.kill(registry, "w1")["state"] == "killed"


def test_send_after_kill_continues_session(registry, workdir, fake_script):
    script = fake_script({"steps": [{"sleep": 60}]}, {"result": "resumed ${session_id}"})
    st = _spawn(registry, workdir, script)
    lifecycle.kill(registry, "w1")
    lifecycle.send(registry, "w1", "carry on")
    done = lifecycle.wait(registry, "w1", timeout_s=20)
    assert done["outcome"] == "exited"
    assert done["last_result"]["text"] == f"resumed {st['session_id']}"


def test_lost_worker_detected(registry, workdir, fake_script):
    _spawn(registry, workdir, fake_script({"steps": [{"sleep": 60}]}))
    inv = registry.invocation_dir("w1", 0)
    deadline = time.monotonic() + 10
    while not (inv / "child.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child = _pid(inv / "child.pid")
    os.kill(_pid(inv / "runner.pid"), signal.SIGKILL)  # runner dies without recording anything
    os.killpg(child, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while tmux.has_session(registry.tmux_name("w1")) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert lifecycle.status(registry, "w1")["state"] == "lost"
    assert lifecycle.kill(registry, "w1")["state"] == "killed"


def test_registry_scoping_and_validation(tmp_path, registry, workdir, fake_script):
    other = Registry(tmp_path / "other")
    assert registry.tmux_name("research") != other.tmux_name("research")
    script = fake_script({})
    _spawn(registry, workdir, script)
    with pytest.raises(AgentctlError, match="already exists"):
        _spawn(registry, workdir, script)
    for bad in ("../escape", "has space", ""):
        with pytest.raises(AgentctlError):
            _spawn(registry, workdir, script, name=bad)
    with pytest.raises(AgentctlError, match="UUID"):
        _spawn(registry, workdir, script, name="w2", session_id="not-a-uuid")
    lifecycle.wait(registry, "w1", timeout_s=20)


def test_list_logs_and_cli(registry, workdir, fake_script, capsys):
    _spawn(registry, workdir, fake_script({"steps": [{"text": "hello"}]}))
    lifecycle.wait(registry, "w1", timeout_s=20)
    assert [r["id"] for r in lifecycle.list_workers(registry)] == ["w1"]
    text = lifecycle.logs(registry, "w1")
    assert "[assistant] hello" in text and "[result] success" in text
    assert lifecycle.logs(registry, "w1", tail=1).startswith("[result]")
    assert lifecycle.logs(registry, "w1", stream="stderr") == ""

    assert cli.main(["--registry", str(registry.root), "status", "w1"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "exited"
    assert cli.main(["status", "--registry", str(registry.root), "missing"]) == 1
    assert "unknown worker" in json.loads(capsys.readouterr().out)["error"]
