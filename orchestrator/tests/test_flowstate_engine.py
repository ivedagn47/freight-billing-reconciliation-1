import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agentctl import lifecycle
from agentctl.registry import Registry
from flowstate import agent_node, cli, engine
from flowstate.errors import FlowstateError
from flowstate.paths import FLOWS_DIR
from flowstate.state import RunStore

from conftest import LIB, requires_tmux
from flowstate_helpers import SCRIPT_FLOW_DOT, SCRIPT_FLOW_FILES, SCRIPT_FLOW_YML, write_flow

SMOKE = str(FLOWS_DIR / "smoke-test" / "smoke-test.dot")
FIX = Path(__file__).parent / "fixtures" / "fake" / "smoke-test"
PROMPT_PATH_RE = "at exactly this path: (\\S+)"
RETRY_PATH_RE = "(\\S+/research-brief\\.json)"  # retry messages list the required output files


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.yml"
    prefs.write_text("supervision: low\nmax_retries: 2\n")
    monkeypatch.setenv("FLOWSTATE_PREFS", str(prefs))
    for var in ("MAKE_FAIL", "MAKE_COUNT", "GATE_FAIL"):
        monkeypatch.delenv(var, raising=False)
    return prefs


@pytest.fixture
def runs(tmp_path):
    return str(tmp_path / "runs")


@pytest.fixture
def cleanup_workers(runs):
    yield
    for run_dir in Path(runs).glob("*"):
        reg = Registry(run_dir / "workers")
        for worker in reg.worker_ids():
            if lifecycle.status(reg, worker)["state"] in ("running", "stalled"):
                lifecycle.kill(reg, worker)


def state_of(runs, run_id="r1"):
    return yaml.safe_load((Path(runs) / run_id / "state.yaml").read_text())


def types(runs, run_id="r1"):
    return [e["type"] for e in engine.events(run_id, runs_dir=runs)]


def init_smoke(runs, research=str(FIX / "research.json"), run_id="r1", **kw):
    return engine.init_run(SMOKE, ["research_topic=tides"], run_id=run_id, runs_dir=runs, harness="fake",
                           fake_scripts={"research": research, "summarise": str(FIX / "summarise.json")}, **kw)


def script_flow(tmp_path, runs, dot=SCRIPT_FLOW_DOT, files=None, run_id="r1"):
    path = write_flow(tmp_path / "flows", "s", dot, SCRIPT_FLOW_YML, files or SCRIPT_FLOW_FILES)
    engine.init_run(str(path), ["greeting=hello"], run_id=run_id, runs_dir=runs)
    return path


def brief(regex=PROMPT_PATH_RE, **overrides):
    """A fake research invocation that writes a brief to the path captured by `regex`."""
    doc = {"_session_id": "${session_id}", "topic": "t", "bullets": ["a", "b", "c"], **overrides}
    return {"steps": [{"capture": {"name": "out", "regex": regex}}, {"write": {"path": "${out}", "json": doc}}]}


# ------------------------------------------------------------------ init

def test_init_creates_layout_state_and_event(runs):
    out = init_smoke(runs)
    run_dir = Path(out["run_dir"])
    for name in ("state.yaml", "events.jsonl", "artefacts", "workers", "logs"):
        assert (run_dir / name).exists()
    st = state_of(runs)
    assert st["status"] == "running" and st["cursor"] == "start" and st["situation"] is None
    assert st["variables"]["research_topic"] == "tides"
    assert st["variables"]["_run_artefact_dir"] == str(run_dir / "artefacts")
    assert {n["status"] for n in st["nodes"].values()} == {"pending"}
    assert st["flow"]["digest"] and st["config"]["harness"] == "fake"
    assert types(runs) == ["run_created"]


@pytest.mark.parametrize("flow_ref,var_pairs,kwargs,code", [
    (SMOKE, [], {}, "missing_variable"),
    (SMOKE, ["research_topic=x", "nope=1"], {}, "unknown_variable"),
    (SMOKE, ["research_topic=x", "research_brief=/tmp/x"], {}, "not_an_input"),
    (SMOKE, ["research_topic"], {}, "invalid_variable"),
    (SMOKE, ["research_topic=x"], {"harness": "fake"}, "fake_script_missing"),
    (str(FLOWS_DIR / "no-such-flow"), [], {}, "flow_not_found"),
])
def test_init_rejects_bad_input_before_creating_a_run(runs, flow_ref, var_pairs, kwargs, code):
    with pytest.raises(FlowstateError) as exc:
        engine.init_run(flow_ref, var_pairs, run_id="bad", runs_dir=runs, **kwargs)
    assert exc.value.code == code
    assert not (Path(runs) / "bad").exists()


def test_init_refuses_existing_run(runs):
    init_smoke(runs)
    with pytest.raises(FlowstateError, match="already exists"):
        init_smoke(runs)


# ------------------------------------------------------------------ script nodes, gates, bindings, routing

def test_script_node_binds_variables_and_passes_gate(tmp_path, runs):
    script_flow(tmp_path, runs)
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "completed", sit
    st = state_of(runs)
    assert st["variables"]["count"] == 2 and st["variables"]["note_path"].endswith("artefacts/note.json")
    assert st["nodes"]["make"]["attempts"][0]["exit_code"] == 0
    logs = Path(runs) / "r1" / "logs"
    assert (logs / "make" / "attempt-1" / "script.stdout.log").exists()
    gate = json.loads((logs / "gates" / "make--done" / "eval-1" / "00-check.result.json").read_text())
    assert gate["exit_code"] == 0
    assert types(runs) == ["run_created", "node_started", "node_completed", "node_started", "outputs_validated",
                           "gate_passed", "node_completed", "run_completed"]
    before = len(types(runs))
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert len(types(runs)) == before  # completed runs never re-execute


def test_script_failure_then_retry(tmp_path, runs, monkeypatch):
    script_flow(tmp_path, runs)
    monkeypatch.setenv("MAKE_FAIL", "1")
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "script_failed" and sit["exit_code"] == 3
    assert "asked to fail" in sit["stderr_tail"] and "respawn" not in sit["options"]
    assert engine.advance("r1", runs_dir=runs)["attempt"] == 1  # pending situation, nothing re-run
    monkeypatch.delenv("MAKE_FAIL")
    assert engine.retry("r1", "make", runs_dir=runs)["retries_used"] == 1
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert [a["kind"] for a in state_of(runs)["nodes"]["make"]["attempts"]] == ["run", "retry"]


def test_gate_failure_blocks_downstream(tmp_path, runs, monkeypatch):
    script_flow(tmp_path, runs)
    monkeypatch.setenv("GATE_FAIL", "1")
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "gate_failed" and sit["exit_code"] == 4 and sit["gate"] == "gates/check.sh"
    assert "gate told to fail" in sit["stderr_tail"]
    st = state_of(runs)
    assert st["cursor"] == "make" and "count" not in st["variables"]  # nothing committed
    monkeypatch.delenv("GATE_FAIL")
    engine.retry("r1", "make", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert state_of(runs)["nodes"]["make"]["gate_evaluations"] == 2


def test_invalid_output_never_moves_downstream(tmp_path, runs, monkeypatch):
    script_flow(tmp_path, runs)
    monkeypatch.setenv("MAKE_COUNT", '"two"')
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "validation_failed"
    assert sit["errors"][0]["at"] == "/count" and sit["evidence"]["rejected_outputs"]
    assert state_of(runs)["cursor"] == "make" and "gate_passed" not in types(runs)


def test_conditions_choose_the_route(tmp_path, runs, monkeypatch):
    dot = SCRIPT_FLOW_DOT.replace("  make -> done [gates=\"gates/check.sh\"]\n", """\
  big   [shape=box, runner=script, script="scripts/noop.sh"]
  small [shape=box, runner=script, script="scripts/noop.sh"]
  make -> big [condition="count > 1"]
  make -> small [condition="count <= 1"]
  big -> done
  small -> done
""")
    files = {**SCRIPT_FLOW_FILES, "scripts/noop.sh": "#!/usr/bin/env bash\ntrue\n"}
    script_flow(tmp_path, runs, dot, files)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert "big" in engine.status("r1", runs_dir=runs)["completed_nodes"]
    assert state_of(runs)["nodes"]["small"]["status"] == "pending"

    monkeypatch.setenv("MAKE_COUNT", "1")
    script_flow(tmp_path / "second", runs, dot, files, run_id="r2")
    engine.advance("r2", runs_dir=runs)
    assert state_of(runs, "r2")["nodes"]["small"]["status"] == "completed"

    none_match = dot.replace("count <= 1", "count < 0").replace("count > 1", "count > 5")
    script_flow(tmp_path / "third", runs, none_match, files, run_id="r3")
    assert engine.advance("r3", runs_dir=runs)["situation"] == "no_route"


def test_interrupted_script_is_reported_not_rerun(tmp_path, runs):
    script_flow(tmp_path, runs)
    store = RunStore(Path(runs) / "r1")
    with store.transaction() as st:  # as if a previous advance died mid-script
        st["cursor"] = "make"
        st["nodes"]["start"]["status"] = "completed"
        st["nodes"]["make"].update(status="running", attempts=[
            {"index": 1, "kind": "run", "started_at": "2026-01-01T00:00:00+00:00",
             "log_dir": str(store.logs / "make" / "attempt-1")}])
    assert engine.advance("r1", runs_dir=runs)["situation"] == "node_interrupted"
    engine.retry("r1", "make", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"


def test_flow_change_after_init_is_detected(tmp_path, runs):
    path = script_flow(tmp_path, runs)
    (path.parent / "scripts" / "make.sh").write_text("#!/usr/bin/env bash\necho changed\n")
    assert engine.advance("r1", runs_dir=runs)["situation"] == "flow_changed"


def test_advance_lease_prevents_two_drivers(tmp_path, runs):
    script_flow(tmp_path, runs)
    with RunStore(Path(runs) / "r1").advance_lease():
        assert engine.advance("r1", runs_dir=runs)["situation"] == "busy"
        with pytest.raises(FlowstateError, match="another"):
            engine.retry("r1", "make", runs_dir=runs)
        assert engine.pause("r1", "while busy", runs_dir=runs)["status"] == "paused"  # needs no lease


def test_cli_json_output_and_errors(tmp_path, runs, capsys):
    script_flow(tmp_path, runs)
    assert cli.main(["--runs-dir", runs, "advance", "r1"]) == 0
    assert json.loads(capsys.readouterr().out)["situation"] == "completed"
    assert cli.main(["status", "r1", "--runs-dir", runs]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "completed" and status["completed_nodes"][-1] == "done"
    assert cli.main(["--runs-dir", runs, "events", "r1", "--tail", "1"]) == 0
    assert [e["type"] for e in json.loads(capsys.readouterr().out)] == ["run_completed"]
    assert cli.main(["--runs-dir", runs, "status", "nope"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "run_not_found"
    assert cli.main(["validate", str(FLOWS_DIR / "smoke-branch")]) == 0
    capsys.readouterr()
    broken = write_flow(tmp_path / "broken", "b", SCRIPT_FLOW_DOT.replace("scripts/make.sh", "x.sh"),
                        SCRIPT_FLOW_YML, SCRIPT_FLOW_FILES)
    assert cli.main(["validate", str(broken)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    with pytest.raises(FlowstateError) as exc:
        engine.init_run(str(broken), ["greeting=hi"], run_id="bad", runs_dir=runs)
    assert exc.value.code == "invalid_flow" and not (Path(runs) / "bad").exists()


# ------------------------------------------------------------------ agent nodes (fake harness via agentctl + tmux)

@requires_tmux
def test_smoke_test_flow_runs_end_to_end(runs, cleanup_workers):
    init_smoke(runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    st = state_of(runs)
    for node, artefact in (("research", "research-brief.json"), ("summarise", "summary.json")):
        att = st["nodes"][node]["attempts"][0]
        doc = json.loads((Path(runs) / "r1" / "artefacts" / artefact).read_text())
        assert doc["_session_id"] == att["session_id"] and att["worker_id"] == node
        prompt = (Path(runs) / "r1" / "workers" / node / "prompt.md").read_text()
        assert f"Your session id is {att['session_id']}" in prompt and "{research_brief}" not in prompt
    assert "> tides" in (Path(runs) / "r1" / "workers" / "research" / "prompt.md").read_text()
    assert types(runs) == [
        "run_created", "node_started", "node_completed",
        "node_started", "worker_started", "worker_completed", "outputs_validated", "gate_passed", "node_completed",
        "node_started", "worker_started", "worker_completed", "outputs_validated", "node_completed",
        "run_completed"]


@requires_tmux
def test_validation_failure_retry_continues_same_conversation(runs, cleanup_workers):
    init_smoke(runs, research=str(FIX / "research-invalid-then-fixed.json"))
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "validation_failed" and sit["errors"][0]["at"] == "/bullets"
    out = engine.retry("r1", "research", feedback="Write 3 to 5 bullets.", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    attempts = state_of(runs)["nodes"]["research"]["attempts"]
    assert [a["kind"] for a in attempts] == ["spawn", "retry"]
    assert {a["session_id"] for a in attempts} == {out["session_id"]}
    assert {a["worker_id"] for a in attempts} == {"research"}
    reg = Registry(Path(runs) / "r1" / "workers")
    assert reg.invocation_count("research") == 2
    message = (reg.invocation_dir("research", 1) / "input.md").read_text()
    assert "/bullets" in message and "Write 3 to 5 bullets." in message and "Required output files:" in message
    assert (Path(runs) / "r1" / "logs" / "research" / "attempt-1" / "rejected" / "research-brief.json").exists()


@requires_tmux
def test_session_id_mismatch_is_rejected(runs, fake_script, cleanup_workers):
    init_smoke(runs, research=fake_script(brief(_session_id="not-this-worker")))
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "validation_failed"
    assert [e["error"] for e in sit["errors"]] == ["session_id_mismatch"]
    assert sit["errors"][0]["actual"] == "not-this-worker"


@requires_tmux
def test_respawn_creates_new_session_and_keeps_old_evidence(runs, tmp_path, cleanup_workers):
    script = tmp_path / "research.json"
    script.write_text(json.dumps({"invocations": [brief(bullets=["only one"])]}))
    init_smoke(runs, research=str(script))
    assert engine.advance("r1", runs_dir=runs)["situation"] == "validation_failed"
    script.write_text(json.dumps({"invocations": [brief()]}))  # the replacement worker behaves
    out = engine.respawn("r1", "research", reason="start clean", runs_dir=runs)
    assert out["worker_id"] == "research.respawn-1" and out["session_id"] != out["old_session_id"]
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    attempts = state_of(runs)["nodes"]["research"]["attempts"]
    assert attempts[1]["replaces_session_id"] == attempts[0]["session_id"]
    workers = Path(runs) / "r1" / "workers"
    assert (workers / "research" / "prompt.md").exists() and (workers / "research.respawn-1" / "prompt.md").exists()
    assert (Path(runs) / "r1" / "logs" / "research" / "attempt-1" / "before-respawn").is_dir()
    event = next(e for e in engine.events("r1", runs_dir=runs) if e["type"] == "respawn_requested")
    assert (event["old_session_id"], event["new_session_id"]) == (attempts[0]["session_id"], out["session_id"])


@requires_tmux
def test_retry_budget_is_enforced(runs, fake_script, cleanup_workers):
    script = fake_script(brief(bullets=["one"]), brief(RETRY_PATH_RE, bullets=["still one"]))
    init_smoke(runs, research=script, max_retries_default=1)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "validation_failed"
    engine.retry("r1", "research", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "validation_failed"
    sit = engine.retry("r1", "research", runs_dir=runs)
    assert sit["situation"] == "retries_exhausted" and sit["max_retries"] == 1
    assert engine.advance("r1", runs_dir=runs)["situation"] == "retries_exhausted"
    with pytest.raises(FlowstateError, match="no retryable"):
        engine.retry("r1", "research", runs_dir=runs)
    assert "retries_exhausted" in types(runs)


@requires_tmux
def test_worker_failure_then_retry(runs, fake_script, cleanup_workers):
    init_smoke(runs, research=fake_script({"exit_code": 2, "is_error": True}, brief(RETRY_PATH_RE)))
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "worker_failed" and sit["worker_state"] == "failed"
    engine.retry("r1", "research", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"


@requires_tmux
def test_stall_is_reported_and_respawn_kills_old_worker(runs, fake_script, cleanup_workers):
    init_smoke(runs, research=fake_script({"steps": [{"text": "thinking"}, {"sleep": 60}]}))
    sit = engine.advance("r1", runs_dir=runs, stall_after_s=1)
    assert sit["situation"] == "worker_stalled" and state_of(runs)["situation"] is None
    engine.respawn("r1", "research", runs_dir=runs)
    reg = Registry(Path(runs) / "r1" / "workers")
    assert lifecycle.status(reg, "research")["state"] == "killed"
    assert lifecycle.status(reg, "research.respawn-1")["state"] in ("running", "stalled")


@requires_tmux
def test_pause_resume_and_pause_at(runs, cleanup_workers):
    init_smoke(runs)
    engine.pause("r1", "hold on", runs_dir=runs)
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "paused" and sit["reason"] == "hold on"
    assert state_of(runs)["cursor"] == "start"
    engine.resume("r1", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"

    init_smoke(runs, run_id="r2", supervision="high")  # smoke-test nodes use pause_at="optional"
    sit = engine.advance("r2", runs_dir=runs)
    assert (sit["situation"], sit["node"], sit["source"]) == ("paused", "research", "pause_at")
    assert not (Path(runs) / "r2" / "workers" / "research").exists()
    engine.resume("r2", runs_dir=runs)
    assert engine.advance("r2", runs_dir=runs)["node"] == "summarise"
    engine.resume("r2", runs_dir=runs)
    assert engine.advance("r2", runs_dir=runs)["situation"] == "completed"
    assert types(runs, "r2").count("paused") == 2 and types(runs, "r2").count("resumed") == 2


@requires_tmux
def test_abort_stops_workers_and_execution(runs, fake_script, cleanup_workers):
    init_smoke(runs, research=fake_script({"steps": [{"sleep": 60}]}))
    assert engine.advance("r1", runs_dir=runs, max_wait_s=1)["situation"] == "worker_running"
    out = engine.abort("r1", "operator stop", runs_dir=runs)
    assert out["killed_workers"] == ["research"]
    assert lifecycle.status(Registry(Path(runs) / "r1" / "workers"), "research")["state"] == "killed"
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "aborted" and sit["reason"] == "operator stop"
    with pytest.raises(FlowstateError, match="aborted"):
        engine.retry("r1", "research", runs_dir=runs)
    assert (Path(runs) / "r1" / "workers" / "research" / "transcript.jsonl").exists()
    assert {"aborted", "worker_killed"} <= set(types(runs))


@requires_tmux
def test_recovery_in_a_new_process_does_not_duplicate_work(runs, fake_script, cleanup_workers):
    slow = {"steps": [{"text": "working"}, {"sleep": 2}] + brief()["steps"]}
    init_smoke(runs, research=fake_script(slow))
    env = {**os.environ, "PYTHONPATH": str(LIB)}

    def cli_advance(*extra):
        proc = subprocess.run([sys.executable, "-m", "flowstate", "--runs-dir", runs, "advance", "r1", *extra],
                              capture_output=True, text=True, env=env, timeout=60)
        return json.loads(proc.stdout)

    assert cli_advance("--max-wait", "0.5")["situation"] == "worker_running"
    assert cli_advance()["situation"] == "completed"
    assert len(state_of(runs)["nodes"]["research"]["attempts"]) == 1
    assert sorted(Registry(Path(runs) / "r1" / "workers").worker_ids()) == ["research", "summarise"]
    assert types(runs).count("worker_started") == 2


@requires_tmux
def test_crash_between_recording_and_spawning_recovers_once(runs, monkeypatch, cleanup_workers):
    init_smoke(runs)

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("simulated crash inside spawn")

    monkeypatch.setattr(agent_node, "spawn", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.advance("r1", runs_dir=runs)
    assert state_of(runs)["nodes"]["research"]["attempts"][0]["launch"] == "pending"
    monkeypatch.undo()
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert len(state_of(runs)["nodes"]["research"]["attempts"]) == 1
