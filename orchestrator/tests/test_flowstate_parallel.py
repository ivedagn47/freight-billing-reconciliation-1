"""Phase 3: fork, join, dynamic_fanout, reducers, branch persistence and recovery."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from agentctl import lifecycle
from agentctl.registry import Registry
from flowstate import cli, engine
from flowstate.errors import FlowstateError
from flowstate.loader import check_flow, load_flow
from flowstate.paths import FLOWS_DIR

from conftest import LIB, requires_tmux
from flowstate_helpers import write_flow

PATH_RE = "at exactly this path: (\\S+)"
NOTE_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
               "additionalProperties": False, "required": ["_session_id", "note"],
               "properties": {"_session_id": {"type": "string"}, "note": {"type": "string"}}}
RESULT_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                 "additionalProperties": False, "required": ["_session_id", "item", "branch_id"],
                 "properties": {"_session_id": {"type": "string"},
                                "item": {"type": "string", "pattern": "^[a-z]+$"},
                                "branch_id": {"type": "string"}}}

# ---------------------------------------------------------------- fork flow: three script branches

FORK_DOT = """
digraph f {
  start [shape=Mdiamond]
  fork  [runner=fork]
  a     [shape=box, runner=script, script="scripts/a.sh", working_dir="{_run_artefact_dir}", output_schema="a_out"]
  b     [shape=box, runner=script, script="scripts/b.sh", working_dir="{_run_artefact_dir}", output_schema="b_out"]
  c     [shape=box, runner=script, script="scripts/c.sh", working_dir="{_run_artefact_dir}", output_schema="c_out"]
  j     [runner=join, reducer_script="scripts/reduce.sh", summary_var="summary"]
  after [shape=box, runner=script, script="scripts/after.sh", working_dir="{_run_artefact_dir}", output_schema="after_out"]
  done  [shape=Msquare]
  start -> fork
  fork -> a
  fork -> b
  fork -> c
  a -> j
  b -> j
  c -> j
  j -> after
  after -> done
}
"""
FORK_YML = """
output_schemas:
  a_out: {files: [{name: note, path: "{_run_artefact_dir}/note.json", definition: note}], sets_variables: {note_a: note}}
  b_out: {files: [{name: note, path: "{_run_artefact_dir}/note.json", definition: note}], sets_variables: {note_b: note}}
  c_out: {files: [{name: note, path: "{_run_artefact_dir}/note.json", definition: note}], sets_variables: {note_c: note}}
  after_out: {files: [{name: merged, path: "{_run_artefact_dir}/merged.json", definition: note}], sets_variables: {merged: merged}}
variables:
  note_a: {type: path}
  note_b: {type: path}
  note_c: {type: path}
  summary: {type: dict}
  merged: {type: path}
"""


def _branch_script(x: str) -> str:
    up = x.upper()
    return f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        sleep "${{{up}_SLEEP:-0}}"
        [ -z "${{{up}_FAIL:-}}" ] || {{ echo "branch {x} told to fail" >&2; exit 5; }}
        jq -n --arg n "$FLOWSTATE_VAR__branch_id" '{{_session_id: "script", note: $n}}' > "$FLOWSTATE_VAR_note_{x}"
        """


FORK_FILES = {
    **{f"scripts/{x}.sh": _branch_script(x) for x in "abc"},
    "scripts/reduce.sh": """\
        #!/usr/bin/env bash
        set -euo pipefail
        [ -z "${REDUCER_FAIL:-}" ] || { echo "reducer told to fail" >&2; exit 7; }
        if [ -n "${REDUCER_BAD:-}" ]; then echo '[]' > "$FLOWSTATE_REDUCER_SUMMARY_OUT"; exit 0; fi
        jq --arg b "$FLOWSTATE_BRANCH_ID" '.order = ((.order // []) + [$b])' "$FLOWSTATE_REDUCER_SUMMARY_IN" \\
          > "$FLOWSTATE_REDUCER_SUMMARY_OUT"
        """,
    "scripts/after.sh": """\
        #!/usr/bin/env bash
        set -euo pipefail
        jq -n --arg n "$FLOWSTATE_VAR_note_a|$FLOWSTATE_VAR_note_b|$FLOWSTATE_VAR_note_c|$FLOWSTATE_VAR_summary" \\
          '{_session_id: "script", note: $n}' > "$FLOWSTATE_VAR_merged"
        """,
}

# ---------------------------------------------------------------- fan-out flow: one agent node per item

FANOUT_DOT = """
digraph t {
  start [shape=Mdiamond]
  fan   [runner=dynamic_fanout, items="items", max_parallel=2]
  work  [shape=box, prompt_template="prompts/work.md", working_dir="{_run_artefact_dir}", output_schema="work_out"]
  j     [runner=join]
  done  [shape=Msquare]
  start -> fan
  fan -> work
  work -> j
  j -> done
}
"""
FANOUT_YML = """
output_schemas:
  work_out: {files: [{name: result, path: "{_run_artefact_dir}/result.json", definition: result}], sets_variables: {result: result}}
variables:
  items: {type: list}
  result: {type: path}
"""
FANOUT_FILES = {"prompts/work.md": "Item: {item}\nBranch: {_branch_id}\nIndex: {_branch_index}\n"
                                   "Write a JSON file at exactly this path: {result}\n"}

# ---------------------------------------------------------------- fork flow: three agent branches

FORK_AGENT_DOT = """
digraph g {
  start [shape=Mdiamond]
  fork  [runner=fork]
  ag_a  [shape=box, prompt_template="prompts/ag_a.md", working_dir="{_run_artefact_dir}", output_schema="ag_a_out"]
  ag_b  [shape=box, prompt_template="prompts/ag_b.md", working_dir="{_run_artefact_dir}", output_schema="ag_b_out"]
  ag_c  [shape=box, prompt_template="prompts/ag_c.md", working_dir="{_run_artefact_dir}", output_schema="ag_c_out"]
  j     [runner=join]
  done  [shape=Msquare]
  start -> fork
  fork -> ag_a
  fork -> ag_b
  fork -> ag_c
  ag_a -> j
  ag_b -> j
  ag_c -> j
  j -> done
}
"""
FORK_AGENT_YML = "output_schemas:\n" + "".join(
    f'  ag_{x}_out: {{files: [{{name: note, path: "{{_run_artefact_dir}}/note.json", definition: note}}], '
    f"sets_variables: {{note_{x}: note}}}}\n" for x in "abc") + "variables:\n" + "".join(
    f"  note_{x}: {{type: path}}\n" for x in "abc")
FORK_AGENT_FILES = {f"prompts/ag_{x}.md": f"Write a JSON file at exactly this path: {{note_{x}}}\n" for x in "abc"}


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.yml"
    prefs.write_text("supervision: low\nmax_retries: 2\n")
    monkeypatch.setenv("FLOWSTATE_PREFS", str(prefs))
    for var in ("A_SLEEP", "B_SLEEP", "C_SLEEP", "A_FAIL", "B_FAIL", "C_FAIL", "REDUCER_FAIL", "REDUCER_BAD"):
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


def fork_flow(tmp_path, dot=FORK_DOT, yml=FORK_YML, files=None, name="f"):
    return write_flow(tmp_path / "flows", name, dot, yml, files or FORK_FILES, {"note": NOTE_SCHEMA})


def fanout_flow(tmp_path, dot=FANOUT_DOT, yml=FANOUT_YML, name="t"):
    return write_flow(tmp_path / "flows", name, dot, yml, FANOUT_FILES, {"result": RESULT_SCHEMA})


def item_worker(fake_script, sleep=0):
    first = [{"capture": {"name": "item", "regex": "(?m)^Item: (.*)$"}},
             {"capture": {"name": "branch", "regex": "(?m)^Branch: (.+)$"}},
             {"capture": {"name": "out", "regex": PATH_RE}}]
    if sleep:
        first.append({"sleep": sleep})
    first.append({"write": {"path": "${out}", "json": {"_session_id": "${session_id}", "item": "${item}",
                                                       "branch_id": "${branch}"}}})
    retry = [{"capture": {"name": "out", "regex": "(\\S+/result\\.json)"}},
             {"write": {"path": "${out}", "json": {"_session_id": "${session_id}", "item": "fixed",
                                                   "branch_id": "retried"}}}]
    return fake_script({"steps": first}, {"steps": retry})


def init_fanout(tmp_path, runs, fake_script, items, run_id="r1", dot=FANOUT_DOT, sleep=0, **kw):
    path = fanout_flow(tmp_path / run_id, dot=dot)
    return engine.init_run(str(path), [f"items={json.dumps(items)}"], run_id=run_id, runs_dir=runs,
                           harness="fake", fake_scripts={"work": item_worker(fake_script, sleep)}, **kw)


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (FileNotFoundError, KeyError, TypeError, IndexError, yaml.YAMLError):
            pass
        time.sleep(0.1)
    raise AssertionError("condition not reached in time")


# ================================================================ A. static validation

def issue_codes(dot_path):
    _, issues = check_flow(dot_path, dot_path.parent / f"{dot_path.stem}.flow.yml")
    return {i["code"] for i in issues.errors}, issues


def test_valid_fork_and_fanout_flows(tmp_path):
    for dot_path in (fork_flow(tmp_path), fanout_flow(tmp_path),
                     write_flow(tmp_path / "flows", "g", FORK_AGENT_DOT, FORK_AGENT_YML, FORK_AGENT_FILES,
                                {"note": NOTE_SCHEMA})):
        found, issues = issue_codes(dot_path)
        assert found == set(), issues
    flow = load_flow(FLOWS_DIR / "smoke-fanout" / "smoke-fanout.dot", FLOWS_DIR / "smoke-fanout" / "smoke-fanout.flow.yml")
    region = flow.regions["fan"]
    assert (region.kind, region.join, region.nodes) == ("dynamic_fanout", "collect", {"describe", "stamp"})
    assert region.produced == {"description", "stamp"}
    fork = load_flow(fork_flow(tmp_path / "x"), fork_flow(tmp_path / "x").parent / "f.flow.yml").regions["fork"]
    assert fork.branches == {"a": {"a"}, "b": {"b"}, "c": {"c"}}


FORK_MUTATIONS = [
    ("missing reducer", lambda d, y, f: (d.replace("scripts/reduce.sh", "scripts/nope.sh"), y, f), "missing_file"),
    ("reducer without summary_var", lambda d, y, f: (d.replace(', summary_var="summary"', ""), y, f), "invalid_join"),
    ("summary_var not a dict", lambda d, y, f: (d, y.replace("summary: {type: dict}", "summary: {type: string}"), f),
     "invalid_join"),
    ("orphan join", lambda d, y, f: (d.replace("  after -> done", '  j2 [runner=join]\n  after -> j2\n  j2 -> done'), y, f),
     "orphan_join"),
    ("join input from outside", lambda d, y, f: (d.replace("  start -> fork", "  start -> fork\n  start -> j"), y, f),
     "invalid_join"),
    ("branches end at different joins", lambda d, y, f: (d.replace("  c -> j", '  j2 [runner=join]\n  c -> j2\n  j2 -> after'), y, f),
     "unmatched_join"),
    ("branch escapes to done", lambda d, y, f: (d.replace("  c -> j", "  c -> done"), y, f), "branch_escapes"),
    ("nested fork", lambda d, y, f: (d.replace("  a -> j", '  fk2 [runner=fork]\n  a -> fk2\n  fk2 -> j'), y, f),
     "nested_parallel"),
    ("overlapping branches", lambda d, y, f: (d.replace("  b -> j", "  b -> a"), y, f), "overlapping_branches"),
    ("entered from outside", lambda d, y, f: (d.replace("  start -> fork", "  start -> fork\n  start -> a"), y, f),
     "branch_entered_from_outside"),
    ("branch variable conflict", lambda d, y, f: (d, y.replace("sets_variables: {note_b: note}", "sets_variables: {note_a: note}"), f),
     "branch_variable_conflict"),
    ("empty branch", lambda d, y, f: (d.replace("  fork -> c", "  fork -> c\n  fork -> j"), y, f), "empty_branch"),
    ("item outside a fan-out", lambda d, y, f: (d, y, {**f, "scripts/a.sh": f["scripts/a.sh"].replace(
        "$FLOWSTATE_VAR__branch_id", "$FLOWSTATE_VAR_item")}), "branch_variable_outside_region"),
    ("branch id after the join", lambda d, y, f: (d, y, {**f, "scripts/after.sh": f["scripts/after.sh"].replace(
        "$FLOWSTATE_VAR_summary", "$FLOWSTATE_VAR__branch_id")}), "branch_variable_outside_region"),
    ("reserved item variable", lambda d, y, f: (d, y + "  item: {type: string}\n", f), "reserved_variable"),
    ("condition on a fork edge", lambda d, y, f: (d.replace("  fork -> a", '  fork -> a [condition="note_a == 1"]'), y, f),
     "invalid_branch_edge"),
    ("fork with one branch", lambda d, y, f: (d.replace("  fork -> b\n", "").replace("  fork -> c\n", "")
                                               .replace("  b -> j\n", "").replace("  c -> j\n", ""), y, f), "invalid_fork"),
    ("bad max_parallel", lambda d, y, f: (d.replace("fork  [runner=fork]", "fork  [runner=fork, max_parallel=0]"), y, f),
     "invalid_attribute"),
]


@pytest.mark.parametrize("label,mutate,expected", FORK_MUTATIONS, ids=[m[0] for m in FORK_MUTATIONS])
def test_invalid_fork_and_join_definitions(tmp_path, label, mutate, expected):
    dot, yml, files = mutate(FORK_DOT, FORK_YML, dict(FORK_FILES))
    found, issues = issue_codes(fork_flow(tmp_path, dot, yml, files))
    assert expected in found, issues


FANOUT_MUTATIONS = [
    ("missing items", lambda d, y: (d.replace('items="items", ', ""), y), "missing_attribute"),
    ("undeclared items variable", lambda d, y: (d.replace('items="items"', 'items="nope"'), y), "undeclared_variable"),
    ("items is not a list", lambda d, y: (d, y.replace("items: {type: list}", "items: {type: string}")), "invalid_fanout"),
    ("two template edges", lambda d, y: (d.replace("  fan -> work", '  work2 [shape=box, runner=script, script="x.sh"]\n'
                                                   "  fan -> work\n  fan -> work2\n  work2 -> j"), y), "invalid_fanout"),
    ("bad max_items", lambda d, y: (d.replace("max_parallel=2", "max_parallel=2, max_items=0"), y), "invalid_attribute"),
    ("template never reaches join", lambda d, y: (d.replace("  work -> j\n  j -> done", "  work -> done\n  j -> done"), y),
     "branch_escapes"),
    ("unknown fan-out attribute", lambda d, y: (d.replace("max_parallel=2", "max_parallel=2, items_from=x"), y),
     "unsupported_attribute"),
]


@pytest.mark.parametrize("label,mutate,expected", FANOUT_MUTATIONS, ids=[m[0] for m in FANOUT_MUTATIONS])
def test_invalid_fanout_definitions(tmp_path, label, mutate, expected):
    dot, yml = mutate(FANOUT_DOT, FANOUT_YML)
    found, issues = issue_codes(fanout_flow(tmp_path, dot, yml))
    assert expected in found, issues


# ================================================================ B/C/F. fork, join and reducer (script branches)

def test_fork_runs_branches_in_parallel_and_merges_by_name(tmp_path, runs, monkeypatch):
    path = fork_flow(tmp_path)
    engine.init_run(str(path), [], run_id="r1", runs_dir=runs)
    monkeypatch.setenv("A_SLEEP", "1.5")  # a arrives last, but must still be folded first
    started = time.monotonic()
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert time.monotonic() - started < 4.0  # branches overlapped rather than running one after another

    st = state_of(runs)
    par = st["parallel"]["fork"]
    assert par["branch_order"] == ["fork-a", "fork-b", "fork-c"] and par["status"] == "completed"
    arrivals = sorted(par["branch_order"], key=lambda b: par["branches"][b]["completed_at"])
    assert arrivals[-1] == "fork-a"
    assert st["variables"]["summary"] == {"order": ["fork-a", "fork-b", "fork-c"]}
    for x in "abc":
        note = Path(st["variables"][f"note_{x}"])
        assert note == Path(runs) / "r1" / "artefacts" / "branches" / f"fork-{x}" / "note.json"  # same name, isolated
        assert json.loads(note.read_text())["note"] == f"fork-{x}"
        assert par["branches"][f"fork-{x}"]["nodes"][x]["attempts"][0]["exit_code"] == 0
    merged = json.loads(Path(st["variables"]["merged"]).read_text())["note"]
    assert merged.count("/branches/fork-") == 3
    assert st["nodes"]["a"] == {"kind": "script", "status": "in_branches", "parallel": "fork"}
    evs = types(runs)
    assert evs.count("branch_created") == 3 and evs.count("branch_completed") == 3
    assert evs.count("reducer_completed") == 3 and evs[-4:] == ["node_started", "outputs_validated",
                                                                "node_completed", "run_completed"]
    assert evs.index("join_merged") < evs.index("join_completed")


def test_one_failed_branch_blocks_the_join_until_retried(tmp_path, runs, monkeypatch, capsys):
    engine.init_run(str(fork_flow(tmp_path)), [], run_id="r1", runs_dir=runs)
    monkeypatch.setenv("B_FAIL", "1")
    sit = engine.advance("r1", runs_dir=runs)
    assert (sit["situation"], sit["branch_id"], sit["node"]) == ("script_failed", "fork-b", "b")
    assert sit["branches"] == {"total": 3, "pending": 0, "running": 0, "awaiting_decision": 1, "completed": 2}
    assert "retry --branch fork-b" in sit["options"]
    st = state_of(runs)
    assert st["cursor"] == "fork" and st["parallel"]["fork"]["join_state"]["status"] == "waiting"
    assert st["nodes"]["after"]["status"] == "pending" and "merged" not in st["variables"]
    assert engine.advance("r1", runs_dir=runs)["branch_id"] == "fork-b"  # still blocked, nothing re-run

    monkeypatch.delenv("B_FAIL")
    assert cli.main(["--runs-dir", runs, "retry", "r1", "b", "--branch", "fork-b"]) == 0
    assert json.loads(capsys.readouterr().out)["branch"] == "fork-b"
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    branches = state_of(runs)["parallel"]["fork"]["branches"]
    assert [a["kind"] for a in branches["fork-b"]["nodes"]["b"]["attempts"]] == ["run", "retry"]
    assert len(branches["fork-a"]["nodes"]["a"]["attempts"]) == 1
    assert len(branches["fork-c"]["nodes"]["c"]["attempts"]) == 1
    assert {"branch_failed", "retry_requested"} <= set(types(runs))


def test_join_waits_and_resumes_after_restart(tmp_path, runs, monkeypatch):
    engine.init_run(str(fork_flow(tmp_path)), [], run_id="r1", runs_dir=runs)
    monkeypatch.setenv("B_SLEEP", "2")
    sit = engine.advance("r1", runs_dir=runs, max_wait_s=0.8)
    assert sit["situation"] == "branches_running"
    st = state_of(runs)
    assert st["parallel"]["fork"]["join_state"]["status"] == "waiting"
    assert st["parallel"]["fork"]["branches"]["fork-b"]["status"] == "running"
    assert st["nodes"]["j"]["status"] != "completed" and "join_waiting" in types(runs)
    env = {**os.environ, "PYTHONPATH": str(LIB)}
    proc = subprocess.run([sys.executable, "-m", "flowstate", "--runs-dir", runs, "advance", "r1"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(proc.stdout)["situation"] == "completed"
    branches = state_of(runs)["parallel"]["fork"]["branches"]
    assert all(len(branches[f"fork-{x}"]["nodes"][x]["attempts"]) == 1 for x in "abc")


def test_reducer_failure_is_a_situation_with_evidence(tmp_path, runs, monkeypatch):
    engine.init_run(str(fork_flow(tmp_path)), [], run_id="r1", runs_dir=runs)
    monkeypatch.setenv("REDUCER_FAIL", "1")
    sit = engine.advance("r1", runs_dir=runs)
    assert (sit["situation"], sit["node"], sit["branch_id"], sit["exit_code"]) == ("reducer_failed", "j", "fork-a", 7)
    assert "reducer told to fail" in sit["stderr_tail"]
    log_dir = Path(sit["evidence"]["log_dir"])
    for name in ("reducer.stdout.log", "reducer.stderr.log", "reducer.result.json", "reducer.exit_code",
                 "reducer.command.json", "summary-in.json", "branch-variables.json"):
        assert (log_dir / name).exists(), name
    assert "ANTHROPIC_API_KEY" not in (log_dir / "reducer.command.json").read_text()
    monkeypatch.delenv("REDUCER_FAIL")
    engine.retry("r1", "j", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    runs_record = state_of(runs)["parallel"]["fork"]["join_state"]["reducer_runs"]
    assert [r["outcome"] for r in runs_record] == ["reducer_failed", "completed", "completed", "completed"]
    assert {"reducer_started", "reducer_failed", "reducer_completed"} <= set(types(runs))


def test_reducer_output_must_be_an_object(tmp_path, runs, monkeypatch):
    engine.init_run(str(fork_flow(tmp_path)), [], run_id="r1", runs_dir=runs)
    monkeypatch.setenv("REDUCER_BAD", "1")
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["situation"] == "reducer_output_invalid" and "JSON object" in sit["message"]
    monkeypatch.delenv("REDUCER_BAD")
    engine.retry("r1", "j", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"


def test_killed_advance_does_not_rerun_a_running_script_branch(tmp_path, runs):
    engine.init_run(str(fork_flow(tmp_path)), [], run_id="r1", runs_dir=runs)
    env = {**os.environ, "PYTHONPATH": str(LIB), "B_SLEEP": "3"}
    proc = subprocess.Popen([sys.executable, "-m", "flowstate", "--runs-dir", runs, "advance", "r1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    def a_and_c_done_b_running():
        branches = state_of(runs)["parallel"]["fork"]["branches"]
        return (branches["fork-a"]["status"] == branches["fork-c"]["status"] == "completed"
                and "runner_pid" in branches["fork-b"]["nodes"]["b"]["attempts"][0])

    wait_for(a_and_c_done_b_running)
    os.kill(proc.pid, signal.SIGKILL)  # flowstate dies; the detached script keeps running
    proc.wait()
    runner = state_of(runs)["parallel"]["fork"]["branches"]["fork-b"]["nodes"]["b"]["attempts"][0]["runner_pid"]
    os.kill(runner, 0)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    branches = state_of(runs)["parallel"]["fork"]["branches"]
    assert all(len(branches[f"fork-{x}"]["nodes"][x]["attempts"]) == 1 for x in "abc")
    assert types(runs).count("branch_created") == 3


# ================================================================ D. dynamic fan-out (agent branches via fake harness)

@requires_tmux
def test_fanout_three_items_render_and_isolate(tmp_path, runs, fake_script, cleanup_workers):
    init_fanout(tmp_path, runs, fake_script, ["a", "b", "c"])
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    st = state_of(runs)
    par = st["parallel"]["fan"]
    assert par["branch_order"] == ["fan-0000", "fan-0001", "fan-0002"] and par["items"] == ["a", "b", "c"]
    assert [par["branches"][b]["context"]["item"] for b in par["branch_order"]] == ["a", "b", "c"]
    results = st["variables"]["result"]
    assert [Path(p).parent.name for p in results] == par["branch_order"]  # list merge in branch order
    for index, (bid, item) in enumerate(zip(par["branch_order"], "abc")):
        doc = json.loads(Path(results[index]).read_text())
        assert (doc["item"], doc["branch_id"]) == (item, bid)
        prompt = (Path(runs) / "r1" / "workers" / f"work.{bid}" / "prompt.md").read_text()
        assert f"Item: {item}\nBranch: {bid}\nIndex: {index}\n" in prompt
        assert doc["_session_id"] == par["branches"][bid]["nodes"]["work"]["attempts"][0]["session_id"]
    rows = engine.status("r1", runs_dir=runs)["parallel"]["fan"]["branches"]
    assert [(r["id"], r["item"], r["status"], r["workers"]) for r in rows] == [
        ("fan-0000", "a", "completed", ["work.fan-0000"]), ("fan-0001", "b", "completed", ["work.fan-0001"]),
        ("fan-0002", "c", "completed", ["work.fan-0002"])]
    evs = types(runs)
    assert evs.count("fanout_created") == 1 and evs.count("branch_created") == 3
    assert evs.count("worker_started") == 3


@requires_tmux
def test_fanout_single_item(tmp_path, runs, fake_script, cleanup_workers):
    init_fanout(tmp_path, runs, fake_script, ["solo"])
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    st = state_of(runs)
    assert st["parallel"]["fan"]["branch_order"] == ["fan-0000"] and len(st["variables"]["result"]) == 1


def test_empty_fanout_skips_straight_to_the_join(tmp_path, runs, fake_script):
    init_fanout(tmp_path, runs, fake_script, [])
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    st = state_of(runs)
    par = st["parallel"]["fan"]
    assert par["branch_order"] == [] and par["join_state"]["status"] == "completed"
    assert st["variables"]["result"] == []
    assert Registry(Path(runs) / "r1" / "workers").worker_ids() == []
    evs = types(runs)
    assert "fanout_empty" in evs and "branch_created" not in evs and "join_completed" in evs


@requires_tmux
def test_one_failing_branch_is_retried_without_rerunning_the_others(tmp_path, runs, fake_script, cleanup_workers):
    init_fanout(tmp_path, runs, fake_script, ["a", "BAD", "c", "d", "e"])
    sit = engine.advance("r1", runs_dir=runs)
    assert (sit["situation"], sit["branch_id"], sit["item"]) == ("validation_failed", "fan-0001", "BAD")
    assert sit["errors"][0]["at"] == "/item"
    assert sit["branches"]["completed"] == 4 and sit["branches"]["awaiting_decision"] == 1
    rejected = Path(runs) / "r1" / "logs" / "branches" / "fan-0001" / "work" / "attempt-1" / "rejected" / "result.json"
    assert json.loads(rejected.read_text())["item"] == "BAD"

    engine.retry("r1", "work", runs_dir=runs)  # only one branch has a situation, so no --branch needed
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    st = state_of(runs)
    reg = Registry(Path(runs) / "r1" / "workers")
    for bid in st["parallel"]["fan"]["branch_order"]:
        expected = 2 if bid == "fan-0001" else 1
        assert reg.invocation_count(f"work.{bid}") == expected
        assert len(st["parallel"]["fan"]["branches"][bid]["nodes"]["work"]["attempts"]) == expected
    assert json.loads(Path(st["variables"]["result"][1]).read_text())["item"] == "fixed"


@requires_tmux
def test_two_failing_branches_need_an_explicit_branch(tmp_path, runs, fake_script, cleanup_workers):
    init_fanout(tmp_path, runs, fake_script, ["a", "BAD", "c", "DOG"])
    sit = engine.advance("r1", runs_dir=runs)
    assert sit["branch_id"] == "fan-0001" and sit["other_branch_situations"] == 1
    with pytest.raises(FlowstateError) as exc:
        engine.retry("r1", "work", runs_dir=runs)
    assert exc.value.code == "branch_required" and exc.value.details["branches"] == ["fan-0001", "fan-0003"]
    engine.retry("r1", "work", runs_dir=runs, branch="fan-0001")
    assert engine.advance("r1", runs_dir=runs)["branch_id"] == "fan-0003"
    engine.retry("r1", "work", runs_dir=runs)
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"


@requires_tmux
def test_partial_completion_is_persisted(tmp_path, runs, fake_script, cleanup_workers):
    init_fanout(tmp_path, runs, fake_script, ["a", "b", "c"], sleep=1.5)  # max_parallel=2
    sit = engine.advance("r1", runs_dir=runs, max_wait_s=0.5)
    assert sit["situation"] == "branches_running" and sit["branches"]["running"] == 2
    assert sit["branches"]["pending"] == 1
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert types(runs).count("worker_started") == 3


@requires_tmux
def test_deterministic_branch_ids_are_never_recreated(tmp_path, runs, fake_script, cleanup_workers):
    items = [f"x{'abcdefghijkl'[i]}" for i in range(12)]
    init_fanout(tmp_path, runs, fake_script, items, dot=FANOUT_DOT.replace("max_parallel=2", "max_parallel=6"))
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    par = state_of(runs)["parallel"]["fan"]
    assert par["branch_order"] == [f"fan-{i:04d}" for i in range(12)]
    created = {b: par["branches"][b]["created_at"] for b in par["branch_order"]}
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"
    assert {b: state_of(runs)["parallel"]["fan"]["branches"][b]["created_at"] for b in created} == created
    assert types(runs).count("branch_created") == 12


def test_fanout_items_from_a_json_file_and_limits(tmp_path, runs, fake_script):
    yml = FANOUT_YML.replace("items: {type: list}", "items: {type: path}")
    path = fanout_flow(tmp_path / "file", yml=yml)
    items_file = tmp_path / "items.json"
    items_file.write_text("[]")
    engine.init_run(str(path), [f"items={items_file}"], run_id="r1", runs_dir=runs, harness="fake",
                    fake_scripts={"work": item_worker(fake_script)})
    assert engine.advance("r1", runs_dir=runs)["situation"] == "completed"

    items_file.write_text('{"not": "a list"}')
    engine.init_run(str(path), [f"items={items_file}"], run_id="r2", runs_dir=runs, harness="fake",
                    fake_scripts={"work": item_worker(fake_script)})
    sit = engine.advance("r2", runs_dir=runs)
    assert sit["situation"] == "fanout_invalid" and sit["options"] == ["abort"]

    init_fanout(tmp_path, runs, fake_script, ["a", "b", "c"], run_id="r3",
                dot=FANOUT_DOT.replace("max_parallel=2", "max_parallel=2, max_items=2"))
    sit = engine.advance("r3", runs_dir=runs)
    assert sit["situation"] == "fanout_invalid" and "max_items=2" in sit["message"]
    assert "parallel" not in state_of(runs, "r3") or state_of(runs, "r3")["parallel"] == {}


@requires_tmux
def test_abort_during_fanout_kills_branch_workers(tmp_path, runs, fake_script, cleanup_workers):
    init_fanout(tmp_path, runs, fake_script, ["a", "b", "c"], sleep=30)
    assert engine.advance("r1", runs_dir=runs, max_wait_s=1.0)["situation"] == "branches_running"
    out = engine.abort("r1", "stop", runs_dir=runs)
    assert sorted(out["killed_workers"]) == ["work.fan-0000", "work.fan-0001"]
    reg = Registry(Path(runs) / "r1" / "workers")
    assert {lifecycle.status(reg, w)["state"] for w in reg.worker_ids()} == {"killed"}
    assert engine.advance("r1", runs_dir=runs)["situation"] == "aborted"


# ================================================================ E. recovery with agent branches

@requires_tmux
def test_killed_advance_reconnects_to_running_branch_worker(tmp_path, runs, fake_script, cleanup_workers):
    path = write_flow(tmp_path / "flows", "g", FORK_AGENT_DOT, FORK_AGENT_YML, FORK_AGENT_FILES, {"note": NOTE_SCHEMA})

    def worker(sleep=0):
        steps = [{"capture": {"name": "out", "regex": PATH_RE}}] + ([{"sleep": sleep}] if sleep else [])
        steps.append({"write": {"path": "${out}", "json": {"_session_id": "${session_id}", "note": "${worker_id}"}}})
        return fake_script({"steps": steps})

    engine.init_run(str(path), [], run_id="r1", runs_dir=runs, harness="fake",
                    fake_scripts={"ag_a": worker(), "ag_b": worker(sleep=4), "ag_c": worker()})
    env = {**os.environ, "PYTHONPATH": str(LIB)}
    proc = subprocess.Popen([sys.executable, "-m", "flowstate", "--runs-dir", runs, "advance", "r1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    def a_and_c_done_b_running():
        branches = state_of(runs)["parallel"]["fork"]["branches"]
        return (branches["fork-ag_a"]["status"] == branches["fork-ag_c"]["status"] == "completed"
                and branches["fork-ag_b"]["nodes"]["ag_b"]["attempts"][0]["launch"] == "launched")

    wait_for(a_and_c_done_b_running)
    os.kill(proc.pid, signal.SIGKILL)  # Case A: kill flowstate while branch B is still working
    proc.wait()
    reg = Registry(Path(runs) / "r1" / "workers")
    assert lifecycle.status(reg, "ag_b.fork-ag_b")["state"] == "running"

    second = subprocess.run([sys.executable, "-m", "flowstate", "--runs-dir", runs, "advance", "r1"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(second.stdout)["situation"] == "completed"
    branches = state_of(runs)["parallel"]["fork"]["branches"]
    for x in ("a", "b", "c"):
        attempts = branches[f"fork-ag_{x}"]["nodes"][f"ag_{x}"]["attempts"]
        assert len(attempts) == 1
    assert sorted(reg.worker_ids()) == ["ag_a.fork-ag_a", "ag_b.fork-ag_b", "ag_c.fork-ag_c"]
    assert all(reg.invocation_count(w) == 1 for w in reg.worker_ids())
    evs = types(runs)
    assert evs.count("worker_started") == 3 and evs.count("branch_created") == 3
